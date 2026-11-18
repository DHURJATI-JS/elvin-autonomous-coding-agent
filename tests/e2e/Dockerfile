FROM elvin-runner:1
USER root
RUN pip install asyncpg==0.32.0
COPY tests/e2e/browser_check.py /e2e/browser_check.py
RUN mkdir -p /out && chown 1001:1001 /out
USER 1001
CMD ["python", "/e2e/browser_check.py"]
