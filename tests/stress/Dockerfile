FROM elvin-app:1
USER root
COPY tests/stress /stress
RUN mkdir -p /stress/out && chown -R 1000:1000 /stress
USER 1000
WORKDIR /stress
