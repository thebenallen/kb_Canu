# -*- coding: utf-8 -*-

import json
import logging
import sys
import traceback
from datetime import datetime
from os import environ

from kb_canu.kb_canuImpl import kb_canu

logger = logging.getLogger(__name__)


class MethodContext(dict):
    """
    Lightweight context object passed to every SDK method call.

    Carries per-request data: auth token, user identity, provenance chain,
    client IP, and call metadata.
    """

    def __init__(self, logger=None):
        self["client_ip"]    = None
        self["user_id"]      = None
        self["authenticated"] = 0
        self["token"]        = None
        self["module"]       = "kb_canu"
        self["method"]       = "none"
        self["call_id"]      = None
        self["rpc_context"]  = None
        self["provenance"]   = []


class Application:
    """WSGI application: routes JSON-RPC calls to kb_canuImpl."""

    def __init__(self):
        config_file = environ.get("KB_DEPLOYMENT_CONFIG")
        self.config = {}
        if config_file:
            import configparser
            cfg = configparser.ConfigParser()
            cfg.read(config_file)
            if cfg.has_section("kb_canu"):
                for k, v in cfg.items("kb_canu"):
                    self.config[k] = v
        self.impl = kb_canu(self.config)

        # Method dispatch table — maps JSON-RPC method name to impl function
        self._dispatch = {
            "kb_canu.run_kb_canu": self.impl.run_kb_canu,
            "kb_canu.status":   self.impl.status,
        }

    # ------------------------------------------------------------------
    # WSGI entry point
    # ------------------------------------------------------------------

    def __call__(self, environ, start_response):
        """Handle one HTTP request."""

        request_method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")

        # ---- Health check endpoint ----------------------------------------
        # GET /status  (or /healthz) returns 200 OK with module version.
        # Used by KBase infrastructure and Docker HEALTHCHECK.
        if request_method == "GET" and path.rstrip("/") in ("/status", "/healthz", ""):
            body = json.dumps({
                "state":   "OK",
                "version": self.impl.VERSION,
                "module":  "kb_canu",
                "time":    datetime.utcnow().isoformat() + "Z",
            }).encode("utf-8")
            start_response("200 OK", [
                ("Content-Type",   "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]

        # ---- JSON-RPC dispatch --------------------------------------------
        body_size = int(environ.get("CONTENT_LENGTH", 0) or 0)
        raw_body  = environ["wsgi.input"].read(body_size)

        try:
            req = json.loads(raw_body)
        except Exception:
            return self._respond(start_response, 400,
                                 self._error(-32700, "Parse error", None))

        method_name = req.get("method", "")
        params      = req.get("params", [{}])
        call_id     = req.get("id")

        # Build context
        ctx = MethodContext()
        ctx["call_id"]   = call_id
        ctx["method"]    = method_name
        ctx["client_ip"] = environ.get("REMOTE_ADDR")

        # Extract auth token from Authorization header
        # KBase clients send:  Authorization: OAuth <token>
        #                  or: Authorization: Bearer <token>
        auth_header = environ.get("HTTP_AUTHORIZATION", "")
        if auth_header.lower().startswith("oauth "):
            ctx["token"]         = auth_header[6:].strip()
            ctx["authenticated"] = 1
        elif auth_header.lower().startswith("bearer "):
            ctx["token"]         = auth_header[7:].strip()
            ctx["authenticated"] = 1

        # Provenance: pick up any context passed in the JSON-RPC envelope
        # (KBase Narrative sends this as req["context"]["provenance"])
        rpc_ctx = req.get("context", {})
        if rpc_ctx.get("provenance"):
            ctx["provenance"] = rpc_ctx["provenance"]
        if rpc_ctx.get("user_id"):
            ctx["user_id"] = rpc_ctx["user_id"]

        # Dispatch
        func = self._dispatch.get(method_name)
        if func is None:
            return self._respond(start_response, 404,
                                 self._error(-32601,
                                             "Method not found: {}".format(method_name),
                                             call_id))

        try:
            p = params[0] if isinstance(params, list) else params
            result = func(ctx, p)
            resp = {"version": "1.1", "result": result, "id": call_id}
            return self._respond(start_response, 200, resp)

        except Exception as exc:
            logger.error("Unhandled error in %s:\n%s",
                         method_name, traceback.format_exc())
            return self._respond(start_response, 500,
                                 self._error(-32603, str(exc), call_id))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _error(code, message, call_id):
        return {
            "version": "1.1",
            "error":   {"code": code, "message": message},
            "id":      call_id,
        }

    @staticmethod
    def _respond(start_response, status_code, body_dict):
        body = json.dumps(body_dict).encode("utf-8")
        phrases = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            500: "Internal Server Error",
        }
        status = "{} {}".format(status_code, phrases.get(status_code, "Unknown"))
        headers = [
            ("Content-Type",   "application/json"),
            ("Content-Length", str(len(body))),
        ]
        start_response(status, headers)
        return [body]
