.PHONY: keys decrypt web build

build:
	cc -O2 -o find_all_keys_macos find_all_keys_macos.c -framework Foundation
	codesign -s - find_all_keys_macos

keys:
	sudo ./find_all_keys_macos

decrypt:
	.venv/bin/python3 main.py decrypt

web:
	.venv/bin/python3 main.py
