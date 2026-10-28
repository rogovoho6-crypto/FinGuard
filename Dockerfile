FROM node:24-alpine AS frontend
WORKDIR /build
RUN corepack enable && corepack prepare pnpm@11.25.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run check && pnpm run build

FROM python:3.11-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 FINGUARD_DATA_DIR=/data
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && mkdir /data && chown 10001:10001 /data
COPY backend/ ./backend/
COPY run.py ./
COPY --from=frontend /build/dist/ ./frontend/dist/
USER 10001:10001
EXPOSE 8765
CMD ["python", "run.py", "--host", "0.0.0.0", "--no-browser"]

FROM runtime AS test
USER root
COPY requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements-test.txt
USER 10001:10001
CMD ["python", "-m", "unittest", "discover", "-s", "backend/tests", "-v"]

FROM runtime AS production
