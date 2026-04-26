FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .

# Run as non-root. Match the user-side ownership of the log PVC mount.
RUN useradd -r -u 1000 appuser
USER appuser

EXPOSE 8080
CMD ["python", "-u", "server.py"]
