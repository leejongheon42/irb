FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IRB_DATA_DIR=/data
WORKDIR /app

# DOCX 자동 작성·HWP/DOC 변환용 LibreOffice와 한글 글꼴
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-nanum \
    && rm -rf /var/lib/apt/lists/*
ENV IRB_SOFFICE_PATH=/usr/bin/soffice

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY irb_automation ./irb_automation
COPY samples ./samples

RUN useradd --system --uid 10001 irb && mkdir -p /data && chown irb /data
USER irb
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1
CMD ["uvicorn", "irb_automation.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
