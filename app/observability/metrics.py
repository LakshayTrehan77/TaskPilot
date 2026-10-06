import time

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request latency in seconds", ["method", "path"]
)
AI_PLAN_GENERATIONS = Counter("ai_plan_generation_total", "AI plan generations started")
AI_PLAN_FAILURES = Counter("ai_plan_generation_failures_total", "AI plan generations that failed")


class MetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status = 500
        started = time.perf_counter()

        async def capture_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            # Route template ("/tasks/{task_id}"), not the raw path, to keep label cardinality low.
            route = scope.get("route")
            path = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(scope["method"], path, str(status)).inc()
            HTTP_DURATION.labels(scope["method"], path).observe(time.perf_counter() - started)
