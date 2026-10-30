"""Security middleware: CSRF double-submit, headers, error messages.

These go through a real Flask app rather than calling the helpers directly,
because the thing worth testing is the wiring - a before_request hook that
never fires, or an exemption prefix that matches more than it should, both
look fine in isolation.
"""

import os
import unittest

# security._client_key imports auth to read the session cookie, and auth
# refuses to load without a long SESSION_SECRET. Set one before the import.
os.environ.setdefault("SESSION_SECRET", "x" * 48)

from flask import Flask, jsonify

import security


def _make_app():
    app = Flask(__name__, template_folder="../templates")
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
    security.init_security(app)

    @app.route("/api/thing", methods=["GET", "POST", "DELETE"])
    def thing():
        return jsonify(ok=True)

    @app.route("/api/admin/thing", methods=["POST"])
    def admin_thing():
        return jsonify(ok=True)

    return app


class CsrfTests(unittest.TestCase):
    def setUp(self):
        self.app = _make_app()
        self.c = self.app.test_client()

    def _token(self):
        """A GET should hand us a token to echo back."""
        self.c.get("/api/thing")
        return self.c.get_cookie(security.CSRF_COOKIE).value

    def test_get_issues_token(self):
        r = self.c.get("/api/thing")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(self.c.get_cookie(security.CSRF_COOKIE))

    def test_post_without_header_is_refused(self):
        self._token()                       # cookie present, header missing
        r = self.c.post("/api/thing")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["reason"], "csrf_failed")

    def test_post_with_matching_header_passes(self):
        tok = self._token()
        r = self.c.post("/api/thing", headers={security.CSRF_HEADER: tok})
        self.assertEqual(r.status_code, 200)

    def test_post_with_wrong_header_is_refused(self):
        self._token()
        r = self.c.post("/api/thing", headers={security.CSRF_HEADER: "not-the-token"})
        self.assertEqual(r.status_code, 403)

    def test_delete_is_also_checked(self):
        self._token()
        self.assertEqual(self.c.delete("/api/thing").status_code, 403)

    def test_no_cookie_no_pass(self):
        """An empty cookie and an empty header must not compare equal."""
        r = self.c.post("/api/thing", headers={security.CSRF_HEADER: ""})
        self.assertEqual(r.status_code, 403)

    def test_admin_routes_are_exempt(self):
        """Header-authenticated, so not CSRF-reachable; must not be blocked."""
        self._token()
        r = self.c.post("/api/admin/thing")
        self.assertEqual(r.status_code, 200)

    def test_non_api_paths_are_not_checked(self):
        r = self.c.post("/unsubscribe")
        self.assertNotEqual(r.status_code, 403)

    def test_log_only_mode_lets_it_through(self):
        original = security.CSRF_ENFORCE
        security.CSRF_ENFORCE = False
        try:
            app = _make_app()
            c = app.test_client()
            c.get("/api/thing")
            self.assertEqual(c.post("/api/thing").status_code, 200)
        finally:
            security.CSRF_ENFORCE = original


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.c = _make_app().test_client()

    def test_csp_has_the_new_directives(self):
        csp = self.c.get("/api/thing").headers["Content-Security-Policy"]
        for directive in ("base-uri 'none'", "form-action 'self'", "object-src 'none'"):
            self.assertIn(directive, csp)

    def test_core_headers_still_present(self):
        h = self.c.get("/api/thing").headers
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])


class ErrorMessageTests(unittest.TestCase):
    def test_413_quotes_the_configured_limit(self):
        app = _make_app()
        with app.test_request_context():
            body, status = app.error_handler_spec[None][413][
                Exception if Exception in app.error_handler_spec[None][413] else
                list(app.error_handler_spec[None][413])[0]
            ](None)
        self.assertEqual(status, 413)
        self.assertIn("12MB", body.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
