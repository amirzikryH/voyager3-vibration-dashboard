#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -W ignore::SyntaxWarning app/server.py --demo
