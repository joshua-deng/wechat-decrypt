.PHONY: keys decrypt web build

PYTHON ?= .venv/bin/python3

build:
	cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
	codesign -s - find_all_keys_macos

keys:
	sudo ./find_all_keys_macos

decrypt:
	$(PYTHON) main.py decrypt

web:
	$(PYTHON) main.py
