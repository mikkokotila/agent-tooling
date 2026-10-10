"""Gate-owned authenticated HTTP, with coordination before every request attempt."""
from __future__ import annotations
import asyncio
import os
import inspect
import json
import hashlib
import time
from urllib.parse import urljoin, urlparse
import httpx
from github_interface.client import GitHubClient as OriginalClient, _raise_for_response
from .quota import Coordinator, account_key, resource_for
from .errors import diagnostic

class GitHubClient(OriginalClient):
    def __init__(self,user=None,base_url='https://api.github.com',*,coordinator=None):
        super().__init__(user,base_url)
        self.coordinator=coordinator or Coordinator()
        self.account=account_key(self.base_url,self.token)

    def metadata(self,path,resource):
        from .collections import context
        scoped=context.get()
        if scoped:return {**scoped,'target':path.split('?')[0],'resource':resource}
        frames=inspect.stack(context=0)
        operation=next((f.function for f in frames if '/caller_pr_gates/' in f.filename and not f.filename.endswith('/transport.py')), 'transport')
        return dict(operation=os.environ.get('CALLER_GATE_OPERATION') or operation,session=os.environ.get('CALLER_GATE_SESSION') or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_SESSION_ID'),
                    process=os.getpid(),target=path.split('?')[0],account=self.account,resource=resource)

    async def _request(self,method,path,headers,retry_transient,follow_redirects,**kwargs):
        resource=resource_for(path);metadata=self.metadata(path,resource)
        attempts=2 if retry_transient else 1
        async with httpx.AsyncClient(base_url=self.base_url,headers=headers,follow_redirects=False,timeout=10) as client:
            for attempt in range(attempts):
                target=path
                for redirect in range(20):
                    url=urlparse(urljoin(self.base_url+'/',target));same_host=url.netloc==urlparse(self.base_url).netloc
                    if url.scheme!='https':raise RuntimeError('Refusing an insecure API redirect')
                    account=self.account if same_host else account_key('https://'+url.netloc,'')
                    resource=resource_for(url.path) if same_host else 'external'
                    metadata=self.metadata(url.path,resource);metadata['account']=account
                    try:permit=self.coordinator.admit(account,resource)
                    except Exception:
                        self.coordinator.audit(**metadata,outcome='cooldown',requests=0);raise
                    try:
                        response=await client.request(method,target,headers={} if same_host else {'Authorization':''},**kwargs)
                        error=diagnostic({'http_status':response.status_code,'message':response.text,'response_body':response.text,
                                          'rate_limit_headers':dict(response.headers)},url.path)
                        if error['kind']=='rate_limited':self.coordinator.throttle(permit,error)
                        elif not self.coordinator.finish(permit,success=response.status_code<400):
                            raise RuntimeError('Recovery lease expired or was superseded; discard late evidence')
                        self.coordinator.audit(**metadata,outcome='fresh',requests=1,status=response.status_code)
                    except httpx.TransportError:
                        self.coordinator.finish(permit,success=False)
                        self.coordinator.audit(**metadata,outcome='transport_error',requests=1)
                        if attempt+1==attempts:raise
                        break
                    except BaseException:
                        self.coordinator.finish(permit,success=False);raise
                    if follow_redirects and response.status_code in {301,302,303,307,308} and response.headers.get('location'):
                        if method.upper() not in {'GET','HEAD'}:raise RuntimeError('Mutation redirect requires reconciliation; not replayed')
                        target=urljoin(str(response.url),response.headers['location']);continue
                    if response.status_code not in {502,503,504} or attempt+1==attempts:return response
                    break
                else:raise RuntimeError('Too many API redirects')
                await asyncio.sleep(.25)

    async def logs(self,path):
        # This uses the same admission path; httpx strips credentials on a
        # cross-origin signed-log redirect. Logs are never cached or retried.
        response=await self._request('GET',path,{'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json'},False,True)
        try:response.raise_for_status()
        except httpx.HTTPStatusError as error:_raise_for_response(response,error)
        return response.text

    async def get(self,path,**kwargs):
        """Authenticated conditional revalidation, never a fallback on failure."""
        headers=dict(kwargs.pop('headers',{}))
        headers.update(Authorization='Bearer '+self.token,Accept='application/vnd.github+json')
        headers['X-GitHub-Api-Version']='2022-11-28'
        key=hashlib.sha256(json.dumps([self.base_url,path,kwargs.get('params'),headers['Accept'],headers['X-GitHub-Api-Version']],sort_keys=True).encode()).hexdigest()
        with self.coordinator.db() as db:
            cached=db.execute('SELECT * FROM representations WHERE account=? AND key=?',(self.account,key)).fetchone()
        if cached and cached['etag']:headers['If-None-Match']=cached['etag']
        started=time.time()
        response=await self._request('GET',path,headers,True,True,**kwargs)
        if response.status_code==304:
            if not cached or not headers.get('If-None-Match'):raise RuntimeError('Conditional response has no complete authenticated representation')
            self.coordinator.audit(**self.metadata(path,resource_for(path)),outcome='revalidated',requests=0,status=304)
            return json.loads(cached['payload'])
        try:response.raise_for_status()
        except httpx.HTTPStatusError as error:_raise_for_response(response,error)
        payload=response.json() if response.content else None
        if response.status_code==200 and response.headers.get('etag') and len(response.content)<=4*1024*1024:
            with self.coordinator.db() as db:
                db.execute("""INSERT INTO representations VALUES(?,?,?,?,?) ON CONFLICT(account,key) DO UPDATE SET
                    etag=excluded.etag,payload=excluded.payload,observed=excluded.observed
                    WHERE excluded.observed>=representations.observed""",
                    (self.account,key,response.headers['etag'],json.dumps(payload),started))
                db.execute('DELETE FROM representations WHERE rowid IN (SELECT rowid FROM representations ORDER BY observed DESC LIMIT -1 OFFSET 2000)')
        return payload
