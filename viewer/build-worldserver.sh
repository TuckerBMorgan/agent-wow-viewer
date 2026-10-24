#!/bin/bash
# Runs once on a disposable x86 Linux builder. Values are supplied by viewer.py.
set -Eeuo pipefail
exec > /var/log/agent-wow-build.log 2>&1
export AWS_DEFAULT_REGION="$REGION"
finish() {
    code=$?
    trap - EXIT
    printf '{"exit_code":%s,"finished_at":"%s"}\n' "$code" "$(date -u +%FT%TZ)" > /tmp/status.json
    aws s3 cp /var/log/agent-wow-build.log "s3://$BUCKET/$BUILD_PREFIX/build.log" || true
    aws s3 cp /tmp/status.json "s3://$BUCKET/$BUILD_PREFIX/status.json" || true
    shutdown -h now
}
trap finish EXIT
dnf install -y docker git
systemctl start docker
mkdir -p /build
cd /build
aws ecr get-login-password | docker login --username AWS --password-stdin "${WORLD_IMAGE%%/*}"
repository=${WORLD_IMAGE#*/}
repository=${repository%:*}
if ! aws ecr describe-images --repository-name "$repository" --image-ids "imageTag=${WORLD_IMAGE##*:}" >/dev/null 2>&1; then
git init core
git -C core remote add origin https://github.com/azerothcore/azerothcore-wotlk.git
git -C core fetch --depth=1 origin 9fc017d9a7eab02d59bb7417eb151b373ea1d153
git -C core checkout --detach FETCH_HEAD
git -C core init modules/mod-watch
git -C core/modules/mod-watch remote add origin https://github.com/agent-wow/mod-watch.git
git -C core/modules/mod-watch fetch --depth=1 origin cb9fa4a8de48c1c76799bd2b2b4607b8c51568a0
git -C core/modules/mod-watch checkout --detach FETCH_HEAD
docker build --target worldserver --build-arg CTYPE=Release --build-arg CTOOLS=0 -f core/apps/docker/Dockerfile -t "$WORLD_IMAGE" core
docker push "$WORLD_IMAGE"
fi
