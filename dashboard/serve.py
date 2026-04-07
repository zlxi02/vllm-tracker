#!/usr/bin/env python3
"""Dev server that serves index.html for SPA routes (/dashboard, /roadmap, etc.)."""
import http.server
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

SPA_ROUTES = {"dashboard", "newsfeed", "roadmap", "resources"}


class SPAHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # Strip leading slash to get the route name
        route = self.path.lstrip("/").split("?")[0].split("#")[0]
        if route in SPA_ROUTES:
            self.path = "/index.html"
        super().do_GET()


http.server.test(HandlerClass=SPAHandler, port=8000)
