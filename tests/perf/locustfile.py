"""
Performance benchmark for the inroad matching endpoint.

Usage:
    pip install locust
    locust -f tests/perf/locustfile.py --host=http://localhost:5001 \
           --users=1000 --spawn-rate=50 --run-time=60s --headless

Target: p99 < 300ms on GET /api/matches/today/<student_id>

Authentication notes
--------------------
* The matches endpoint uses @require_session (signed cookie "ccc_session"),
  NOT a JWT Bearer header.
* /auth/refresh reads the httponly "ccc_refresh" cookie — Locust's built-in
  cookie jar handles both cookies automatically once they are set by /auth/verify.
* DEV_MODE must be enabled on the server so that /auth/magic-link returns a
  "dev_token" in its JSON body (no actual email is sent).
"""

import random

from locust import HttpUser, between, events, task


class MatchingUser(HttpUser):
    wait_time = between(0.5, 2.0)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                             #
    # ------------------------------------------------------------------ #

    def on_start(self):
        """Authenticate via the DEV magic-link flow, then fetch student_id."""
        self.access_token = None
        self.student_id = None

        # 1. Request a magic link — DEV_MODE returns dev_token in JSON body.
        email = f"perftest_{random.randint(1, 100_000)}@example.com"
        resp = self.client.post(
            "/auth/magic-link",
            json={"email": email},
            name="/auth/magic-link (setup)",
        )
        if resp.status_code != 200:
            # Cannot continue without a token — abort this virtual user.
            self.environment.runner.quit()
            return

        dev_token = resp.json().get("dev_token")
        if not dev_token:
            # Server is not in DEV_MODE; benchmark cannot proceed.
            self.environment.runner.quit()
            return

        # 2. Verify the magic-link token.
        #    /auth/verify sets both "ccc_session" (cookie) and returns an
        #    access_token in JSON, and sets the "ccc_refresh" httponly cookie.
        #    Locust's session cookie jar stores all Set-Cookie headers.
        resp2 = self.client.get(
            f"/auth/verify?token={dev_token}",
            name="/auth/verify (setup)",
        )
        if resp2.status_code != 200:
            return

        data = resp2.json()
        self.access_token = data.get("access_token", "")

        # 3. Resolve student_id from /api/me (JWT-protected endpoint).
        me_resp = self.client.get(
            "/api/me",
            headers={"Authorization": f"Bearer {self.access_token}"},
            name="/api/me (setup)",
        )
        if me_resp.status_code == 200:
            self.student_id = me_resp.json().get("id")

    # ------------------------------------------------------------------ #
    # Tasks                                                                 #
    # ------------------------------------------------------------------ #

    @task(10)
    def get_matches_today(self):
        """Primary task: fetch today's matches (session-cookie auth)."""
        if not self.student_id:
            return
        # No explicit Authorization header needed — "ccc_session" cookie is
        # sent automatically by the Locust HTTP session.
        self.client.get(
            f"/api/matches/today/{self.student_id}",
            name="/api/matches/today/[id]",
        )

    @task(2)
    def get_profile(self):
        """Secondary task: fetch own profile (JWT auth)."""
        if not self.access_token:
            return
        self.client.get(
            "/api/me",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )

    @task(1)
    def refresh_token(self):
        """Occasional token refresh — rotates the ccc_refresh cookie."""
        # The "ccc_refresh" httponly cookie is sent automatically; a 401 here
        # just means no valid cookie yet, which is safe to ignore.
        resp = self.client.post("/auth/refresh")
        if resp.status_code == 200:
            # Update stored access_token so subsequent tasks stay authenticated.
            new_token = resp.json().get("access_token")
            if new_token:
                self.access_token = new_token


# ------------------------------------------------------------------ #
# Reporting                                                             #
# ------------------------------------------------------------------ #

@events.quitting.add_listener
def on_quit(environment, **kwargs):
    """Print a p99 pass/fail summary when the run ends."""
    stats = environment.runner.stats.total

    p95 = stats.get_response_time_percentile(0.95) or 0
    p99 = stats.get_response_time_percentile(0.99) or 0
    target = 300

    print("\n=== Performance Summary ===")
    print(f"Requests:        {stats.num_requests}")
    print(f"Failures:        {stats.num_failures}")
    print(f"Median (ms):     {stats.median_response_time}")
    print(f"p95 (ms):        {p95}")
    print(f"p99 (ms):        {p99}")
    print(f"Max (ms):        {stats.max_response_time}")
    print(f"Target:          p99 <= {target}ms")

    if p99 <= target:
        print(f"PASS  p99 {p99}ms <= {target}ms target")
    else:
        print(f"FAIL  p99 {p99}ms > {target}ms target")
        raise SystemExit(1)
