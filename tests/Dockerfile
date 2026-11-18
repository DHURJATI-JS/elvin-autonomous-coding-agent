FROM elvin-app:1
USER root
RUN pip install pytest==9.2.0 pytest-asyncio==1.4.1
COPY elvin /app/elvin
COPY tests /app/tests
COPY install /app/install
COPY pytest.ini /app/pytest.ini
RUN chown -R 1000:1000 /app
USER 1000
WORKDIR /app
