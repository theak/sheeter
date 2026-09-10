# Alpine.  Every requirement has a musllinux wheel on both amd64 and arm64, so nothing
# is built from source here.  Keep it that way: an SDK that pulls in a Rust extension
# with no musl wheel (jiter, tiktoken, tokenizers) means a toolchain in the image.
FROM python:3.13-alpine
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ADD requirements.txt /app/
# --only-binary keeps that honest: if a musl wheel ever goes missing the build fails
# here instead of quietly compiling, or quietly resolving back to an older release.
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
# A wheel that installs is not a wheel that loads.  The musl builds carry their own
# libstdc++, libgcc, libgfortran and openblas, so the only thing they want from the base
# image is libz, which alpine already has for apk.  Import the compiled modules here so
# that if any of it stops being true the build fails rather than the first upload.
RUN python -c "import numpy, scipy.ndimage, PIL.Image, pillow_heif, bottle, waitress"
COPY sheeter/ /app/sheeter/
COPY static/ /app/static/
COPY templates/ /app/templates/
ADD app.py /app/
# And the app itself imports, which covers the templates and the routes.
RUN python -c "import app"
# Analyses live on a volume so a container restart keeps the user's history.
ENV SHEETER_DATA_DIR=/data
VOLUME /data
ENV PORT=5000
EXPOSE 5000
# sh so PORT expands, exec so waitress is PID 1 and gets the stop signal itself.
CMD ["sh", "-c", "exec waitress-serve --host 0.0.0.0 --port ${PORT:-5000} app:app"]
