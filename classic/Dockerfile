FROM public.ecr.aws/docker/library/python:3.12-slim
LABEL org.opencontainers.image.title="nordevent-ne-classic"
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd -r -u 10001 app && mkdir -p /data /shared && chown app /data /shared
COPY common/nelib.py ./
COPY catalog/app.py ./catalog_app.py
COPY booking/app.py ./booking_app.py
COPY classic/app.py ./
USER app
EXPOSE 8000 8002
CMD ["python", "app.py"]
