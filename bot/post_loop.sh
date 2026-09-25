#!/usr/bin/env bash
# 時刻が来た投稿を Bluesky と Threads に出す。HOURS>0 なら、その時間のあいだ5分おきに見て出し続ける
# （GitHubの定時実行はあてにならないので、Macのタスクが1日数回これを起動して、数時間ぶんをこの中で待つ）。
set -u
end=$(( $(date +%s) + ${HOURS:-0} * 3600 ))
fail=0
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
while true; do
  git fetch -q origin main && git reset -q --hard origin/main   # ほかの実行の「投稿済み・投稿中」を取り込む
  python bot/post_bsky.py || fail=1
  python bot/post_threads.py || fail=1
  git add bot/out
  if ! git diff --cached --quiet; then
    git commit -qm "post $(date '+%Y-%m-%d %H:%M') (actions)"
    for i in 1 2 3; do git pull --rebase -X theirs -q origin main && git push -q origin main && break; git rebase --abort 2>/dev/null || true; sleep 5; done
  fi
  [ "$(date +%s)" -ge "$end" ] && break
  sleep 300
done
exit $fail
