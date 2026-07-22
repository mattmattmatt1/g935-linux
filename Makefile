.PHONY: test install-user install-udev uninstall-user compile

test:
	python3 -m unittest discover -s tests -v

compile:
	python3 -m compileall -q g935 g935-control.py g935-dspd.py tools

install-user:
	./install.sh --user

install-udev:
	./install.sh --udev

uninstall-user:
	./install.sh --uninstall-user
