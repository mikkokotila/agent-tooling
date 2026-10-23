"""Route only a gate-owned gh child through an ephemeral Unix HTTP socket.

gh's supported http_unix_socket setting retains its command/parser behavior.
The relay checks each HTTP request, including those inside compound commands;
it verifies upstream TLS normally. No global config, certificates or tokens
are written. The private socket and child-only config disappear on return.
"""
from __future__ import annotations
import hashlib
import http.server
import json
import os
import re
from pathlib import Path
import socket
import socketserver
import subprocess
import tempfile
import threading
import httpx
from .errors import diagnostic
from .quota import Coordinator, CoolingDown, account_key, resource_for

def local(command):
    child=subprocess.Popen(['gh',*command],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    out,error=child.communicate(timeout=10)
    if child.returncode:raise RuntimeError('Local GitHub CLI credentials/settings are unavailable')
    return out.strip()

def credentials():
    user = os.environ.get('GITHUB_INTERFACE_USER')
    return local(['auth', 'token', *(['--user', user] if user else [])])
def settings():return dict(line.split('=',1) for line in local(['config','list']).splitlines() if '=' in line)

OWNER_LOGIN='mikkokotila'

def query_login(token):
    """Login behind exactly this token; gh resolves GH_TOKEN, never the ambient account."""
    child=subprocess.Popen(['gh','api','user','--jq','.login'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                           env={**os.environ,'GH_TOKEN':token})
    try:out,error=child.communicate(timeout=10)
    except BaseException:
        child.kill();child.wait()
        raise
    if child.returncode:raise RuntimeError(f'GitHub account lookup failed: {error.strip()}')
    return out.strip()

def token_login(token=None):
    """Login behind the admitted token, cached per token so writes add no lookup per request."""
    token=credentials() if token is None else token
    key=hashlib.sha256(token.encode()).hexdigest()
    directory=Path(os.environ.get('CALLER_PR_GATES_STATE_DIR') or Path.home()/'.local/state/caller-pr-gates')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/'github-identity.json'
    try:cached=json.loads(path.read_text())
    except (OSError,ValueError):cached={}
    if not isinstance(cached,dict):cached={}
    login=cached.get(key)
    if isinstance(login,str) and login:return login
    coordinator=Coordinator()
    permit=coordinator.admit(account_key('https://api.github.com',token),resource_for('/user'))
    try:
        login=query_login(token)
        if not isinstance(login,str) or not login:raise RuntimeError('GitHub account lookup returned no login')
    except Exception as error:
        if diagnostic(error)['kind']=='rate_limited':coordinator.throttle(permit,error)
        else:coordinator.finish(permit,success=False)
        raise RuntimeError('Authenticated account lookup failed; refusing the GitHub write without a verified owner identity') from error
    # The verified login stands even when concurrent coordination moved on.
    coordinator.finish(permit,success=True)
    cached[key]=login
    path.write_text(json.dumps(cached,sort_keys=True));path.chmod(0o600)
    return login

def require_owner(token=None):
    login=token_login(token)
    if login.lower()!=OWNER_LOGIN.lower():
        raise ValueError(f"GitHub writes require the owner identity '{OWNER_LOGIN}'; the active token belongs to '{login}'. Switch gh to the owner account (gh auth switch --user {OWNER_LOGIN}) and retry.")

def _graphql_code(document):
    """GraphQL source without strings/comments; None when a string never closes."""
    out=[];index=0;end=len(document)
    while index<end:
        char=document[index]
        if char=='#':
            stop=end
            for term in ('\n','\r'):
                found=document.find(term,index)
                if found>=0:stop=min(stop,found)
            index=end if stop>=end else stop+1
            out.append('\n')
        elif char=='"':
            if document.startswith('"""',index):
                # A backslash before the delimiter escapes it even when that
                # backslash is itself escaped; only a bare delimiter closes.
                index+=3;closed=False
                while index<end:
                    if document[index]=='\\' and document.startswith('"""',index+1):index+=4;continue
                    if document.startswith('"""',index):index+=3;closed=True;break
                    index+=1
            else:
                index+=1;closed=False
                while index<end:
                    if document[index]=='\\':index+=2;continue
                    if document[index]=='"':index+=1;closed=True;break
                    index+=1
            if not closed:return None
            out.append('""')
        else:out.append(char);index+=1
    return ''.join(out)

def _graphql_read_only(body):
    """Only documents without mutation/subscription operations; anything else is a write."""
    if len(body)>1024*1024:return False
    try:document=json.loads(body).get('query','')
    except (ValueError,TypeError,AttributeError):return False
    if not isinstance(document,str) or not document:return False
    code=_graphql_code(document)
    if code is None:return False
    for match in re.finditer(r'\b(mutation|subscription)\b',code):
        # An operation keyword is never preceded by another operation keyword;
        # that position holds the operation's name instead. Fragment spreads,
        # fragment definitions, and type conditions name fragments, never
        # operations. A dollar prefix, even across separators, is a variable
        # definition or use. A colon after the keyword is a field alias.
        # Anything else is a real operation.
        if re.search(r'(\.\.\.|\b(query|mutation|subscription|fragment|on))[\s,]*$',code[:match.start()]):continue
        if re.search(r'\$[\s,]*$',code[:match.start()]):continue
        if not re.match(r'[\s,]*:',code[match.end():]):return False
    return True

def authorize_request(token,method,path='',body=b''):
    if method in {'GET','HEAD'}:return
    if method=='POST' and path.split('?')[0].rstrip('/')=='/graphql' and _graphql_read_only(body):return
    require_owner(token)

class UnixServer(socketserver.ThreadingMixIn,http.server.HTTPServer):
    address_family=socket.AF_UNIX;daemon_threads=True
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name='local';self.server_port=0

def run(argv,*,runner=None,coordinator=None,upstream=None,**kwargs):
    coordinator=coordinator or Coordinator();token=credentials()
    if not token:raise RuntimeError('No active GitHub credential; no requests started')
    config=settings();operation='cli:'+':'.join(argv[1:3]);session=os.environ.get('CALLER_GATE_SESSION') or os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_SESSION_ID')
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def address_string(self):return 'local'
        def body(self):
            if self.headers.get('Transfer-Encoding','').lower()=='chunked':
                chunks=[];size=0
                while True:
                    count=int(self.rfile.readline().split(b';')[0],16)
                    if not count:
                        while self.rfile.readline() not in {b'\r\n',b'\n',b''}:pass
                        return b''.join(chunks)
                    size+=count
                    if size>16*1024*1024:raise ValueError('Request body exceeds limit')
                    chunks.append(self.rfile.read(count));self.rfile.read(2)
            count=int(self.headers.get('Content-Length','0'))
            if count<0 or count>16*1024*1024:raise ValueError('Request body exceeds limit')
            return self.rfile.read(count)
        def respond(self,status,body,headers=None):
            self.send_response(status)
            for name,value in (headers or {}).items():
                if name.lower() not in {'content-length','transfer-encoding','content-encoding','connection'}:self.send_header(name,value)
            self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers()
            self.wfile.write(body);self.close_connection=True
        def forward(self):
            permit=None;metadata={};settlement_attempted=False
            try:
                host=self.headers.get('Host','').lower()
                if host!='api.github.com':raise ValueError('Gate relay only accepts the configured GitHub API host')
                auth=self.headers.get('Authorization','');actual=auth.split(' ',1)[-1]
                if actual!=token:raise ValueError('Child request credential differs from the admitted credential')
                resource=resource_for(self.path);account=account_key('https://'+host,actual)
                metadata=dict(operation=operation,session=session,process=os.getpid(),target=self.path.split('?')[0],account=account,resource=resource)
                body=self.body()
                try:authorize_request(token,self.command,self.path,body)
                except ValueError as error:
                    self.respond(403,json.dumps({'message':str(error)}).encode(),{'Content-Type':'application/json'})
                    return
                permit=coordinator.admit(account,resource)
                headers={k:v for k,v in self.headers.items() if k.lower() not in {'host','content-length','transfer-encoding','connection'}}
                response=(upstream or httpx.request)(self.command,'https://'+host+self.path,headers=headers,content=body,timeout=15,follow_redirects=False)
                error=diagnostic({'http_status':response.status_code,'message':response.text,'response_body':response.text,'rate_limit_headers':dict(response.headers)},self.path)
                settlement_attempted=True
                if error['kind']=='rate_limited':coordinator.throttle(permit,error)
                elif not coordinator.finish(permit,success=response.status_code<400):raise RuntimeError('Request lease superseded; discard late evidence')
                coordinator.audit(**metadata,outcome='fresh',requests=1,status=response.status_code)
                self.respond(response.status_code,response.content,dict(response.headers))
            except CoolingDown as error:
                coordinator.audit(**metadata,outcome='cooldown',requests=0)
                self.respond(error.args and int(str(error).split()[1].rstrip(':')) or 403,json.dumps({'message':str(error)}).encode(),{'Content-Type':'application/json'})
            except BaseException:
                if permit and not settlement_attempted:
                    try:coordinator.finish(permit,success=False)
                    except Exception:
                        # Unsettled leases keep their existing fencing/expiry.
                        # Failed cleanup must not suppress the uncertainty response.
                        pass
                self.respond(502,b'{"message":"Gate request relay failed; accounting may be incomplete and mutation outcome may be unknown, reconcile before retry"}',{'Content-Type':'application/json'})
        do_GET=do_POST=do_PATCH=do_PUT=do_DELETE=do_HEAD=forward
    # A short path is required by macOS AF_UNIX. Never copy hosts.yml/tokens.
    with tempfile.TemporaryDirectory(prefix='cpg-',dir='/tmp') as directory:
        path=Path(directory);socket_path=str(path/'api.sock')
        config['http_unix_socket']=socket_path
        (path/'config.yml').write_text(''.join(str(k)+': '+json.dumps(v)+'\n' for k,v in config.items()))
        (path/'config.yml').chmod(0o600)
        server=UnixServer(socket_path,Handler);os.chmod(socket_path,0o600)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True);thread.start()
        env=dict(kwargs.pop('env',os.environ),GH_CONFIG_DIR=directory,GH_TOKEN=token,GH_NO_UPDATE_NOTIFIER='1',GH_TELEMETRY='0')
        # Upstream flags/args remain exactly those passed by the gateway.
        try:return (runner or subprocess.run)(argv,env=env,**kwargs)
        finally:server.shutdown();server.server_close();thread.join(timeout=2)
