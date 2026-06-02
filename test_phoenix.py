import os
os.environ['PHOENIX_CLIENT_HEADERS'] = f"api_key={os.environ['PHOENIX_API_KEY']}"
os.environ['PHOENIX_COLLECTOR_ENDPOINT'] = 'https://app.phoenix.arize.com/s/c2303372901'

from phoenix.otel import register
tp = register(project_name='rapid-agent')   # 不开 auto_instrument,不碰 genai

from opentelemetry import trace
tracer = trace.get_tracer(__name__)
with tracer.start_as_current_span("kill-switch-test") as span:
    span.set_attribute("note", "phoenix connectivity check")

tp.force_flush()
print("flushed — 去 Phoenix 看 rapid-agent 里有没有 kill-switch-test 这条 span")
