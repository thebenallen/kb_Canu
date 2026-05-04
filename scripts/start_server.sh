#!/bin/bash
cd /kb/module
export PYTHONPATH=/kb/module/lib:$PYTHONPATH
gunicorn --worker-class gevent --timeout 300 -b 0.0.0.0:5000 kb_canu.kb_canuServer:application
