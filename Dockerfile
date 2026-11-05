FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m py_compile h1.py && python -m unittest discover -s tests -v
EXPOSE 8000
CMD ["sh","-c","uvicorn h1:app --host 0.0.0.0 --port ${PORT:-8000}"]
