# Docling Parser Lambda — Container Image
# ----------------------------------------
# EFS is mounted at /mnt/tmp by Lambda configuration (see CDK DoclingStack).
# On first cold start, Docling downloads ML model weights (~1.5 GB) to
# /mnt/tmp/docling-models — subsequent warm starts reuse the EFS cache.

FROM public.ecr.aws/lambda/python:3.11

# System dependencies for Docling:
#   - libGL + mesa-libGL: required by OpenCV (used by Docling's table detector)
#   - libglib2.0: required by GLib / GObject (OpenCV dependency)
RUN dnf install -y \
        mesa-libGL \
        glib2 \
        libgomp \
    && dnf clean all

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Lambda handler
COPY handler.py .

# Lambda entry point
CMD ["handler.lambda_handler"]
