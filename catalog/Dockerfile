FROM public.ecr.aws/docker/library/python:3.12-slim
LABEL org.opencontainers.image.title="nordevent-catalog" org.opencontainers.image.vendor="NordEvent Lab (TalTech ICM0014)"
ENV PYTHONUNBUFFERED=1 SERVICE_NAME=catalog
WORKDIR /app
RUN useradd -r -u 10001 app && mkdir -p /data /shared && chown app /data /shared
COPY common/nelib.py catalog/app.py ./
USER app
EXPOSE 8000
CMD ["python", "app.py"]
