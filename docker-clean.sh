#!/usr/bin/env bash

echo "🔍 Removing stopped containers older than 24h..."
docker container prune --force --filter "until=24h"

echo "🧹 Removing unused volumes older than 24h..."
docker volume ls -qf "dangling=true" | while read volume; do
  created_at=$(docker inspect "$volume" --format '{{ .CreatedAt }}' 2>/dev/null)
  if [[ -n "$created_at" ]] && [[ $(date -d "$created_at" +%s) -lt $(date -d '24 hours ago' +%s) ]]; then
    docker volume rm "$volume"
  fi
done

echo "🧼 Removing dangling images older than 24h..."
docker image prune --force --filter "until=24h"

echo "🔌 Removing unused networks..."
docker network prune --force

echo "🧱 Cleaning up build cache older than 24h..."
docker builder prune --force --filter "until=24h"
