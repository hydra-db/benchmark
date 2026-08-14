FROM python:3.12-slim

WORKDIR /work
COPY scripts/compose-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY scripts/paired_bolt_bench.py /work/scripts/paired_bolt_bench.py
ENTRYPOINT ["python", "/work/scripts/paired_bolt_bench.py"]
