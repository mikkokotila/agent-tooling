PYTHON ?= python3.13
.PHONY: build verify test install rollback status
build:
	$(PYTHON) tools/manage.py build
verify test:
	$(PYTHON) tools/manage.py verify
install:
	$(PYTHON) tools/manage.py install
rollback:
	$(PYTHON) tools/manage.py rollback
status:
	$(PYTHON) tools/manage.py status
