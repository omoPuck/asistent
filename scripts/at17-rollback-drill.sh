#!/usr/bin/env bash
#
# AT-17 回滚演练 + 设备侧安全巡检。
#
# 为什么要它：SPEC §21.8 AT-17 要求"回滚演练可复现"，而回滚**必须能指名回滚到哪个冻结版本**。
# 第一次跑这个脚本时它失败了，暴露的是一个真问题：契约标签从来没被推到设备（dev 侧只推 main），
# 于是设备上 `git tag --list 'contracts-*'` 是空的，脚本只能退化到"某个旧提交"
# ——它挑中了仓库骨架（那个提交连 tests/ 都没有），报出"回滚目标不可用"。
# 修法是两步：scripts/deploy.ps1 现在把契约冻结标签一起推；本脚本优先用标签、其次用 main~1。
#
# 行为边界（可审计）：只在 /tmp/at17-rollback 克隆里 checkout；只读部署仓库（status/rev-parse）；
# 不写 ~/code/asistent，不装任何东西，不用 sudo，不碰 ~/.dsh、~/miniforge3。
#
# 用法：scp scripts/at17-rollback-drill.sh board:/tmp/ && ssh board 'sed -i "s/\r$//" /tmp/at17-rollback-drill.sh && bash /tmp/at17-rollback-drill.sh'
set -u

echo "===== 0. 设备与仓库事实 ====="
uname -a
/usr/bin/python3 -VV
echo "repo: $(git -C "$HOME/code/asistent" rev-parse HEAD) $(git -C "$HOME/code/asistent" log -1 --format=%s)"
echo "tags on device: $(git -C "$HOME/code/asistent" tag --list 'contracts-*' | tr '\n' ' ')"
echo "main 的父提交: $(git -C "$HOME/code/asistent" log --oneline -2 | tail -1)"
echo

echo "===== 1. 敏感目录未被触碰（只读检查） ====="
for p in "$HOME/asistent-dev" "$HOME/.dsh" "$HOME/miniforge3" "$HOME/asistent-spike"; do
  if [ -e "$p" ]; then
    echo "[present] $p  mtime=$(stat -c '%y' "$p" 2>/dev/null)"
  else
    echo "[absent]  $p"
  fi
done
echo "run-data: $(stat -c '%a %U %n' "$HOME/asistent-var" 2>/dev/null || echo 'missing')"
echo

echo "===== 2. 回滚演练：在 /tmp 的克隆里检出历史版本并跑测试 ====="
DRILL=/tmp/at17-rollback
rm -rf "$DRILL"
git clone -q "$HOME/code/asistent" "$DRILL"
cd "$DRILL" || exit 1
echo "clone HEAD: $(git rev-parse --short HEAD)"

# 2.1 回滚目标 = **最新的契约冻结标签**（不是写死的某个版本）。
#
# 为什么改成"取最新"：写死 `contracts-v0.7` 意味着 v0.8/v0.9 冻结之后，这个演练还在证明
# 一个两版之前的提交能回滚——而真正要回滚时，目标永远是最近那次冻结。
# 顺带加一条一致性断言：最新标签必须等于 `version.py` 里的 `CONTRACT_VERSION`，
# 这样"只改版本号、忘了打标签"（或反过来）会被演练直接抓出来。
NEWEST_TAG=$(git tag --list 'contracts-v*' | sort -V | tail -1)
VERSION_IN_CODE=$(sed -n 's/^CONTRACT_VERSION = "\(.*\)"$/\1/p' asistent/contracts/version.py | head -1)
echo "--- 最新冻结标签: ${NEWEST_TAG:-（无）} / version.py: ${VERSION_IN_CODE:-（读不到）}"
if [ -z "$NEWEST_TAG" ]; then
  echo "[FAIL] 设备上没有任何契约冻结标签——回滚目标无法指名（这正是本演练第一次跑出来的问题）"
elif [ "contracts-$VERSION_IN_CODE" = "$NEWEST_TAG" ]; then
  echo "[OK]   最新标签与 version.py 一致：$NEWEST_TAG"
else
  echo "[FAIL] 最新标签 $NEWEST_TAG 与 version.py（contracts-$VERSION_IN_CODE）不一致：冻结与代码已漂移"
fi

OLD=$(git rev-parse --verify "${NEWEST_TAG:-main~1}^{commit}" 2>/dev/null || git rev-parse main~1)
echo "--- checkout 历史提交 $(git rev-parse --short "$OLD") ($(git log -1 --format=%s "$OLD" | cut -c1-60))"
git checkout -q "$OLD"
HEAD_OLD=$(git rev-parse --short HEAD)
if PYTHONUTF8=1 PYTHONIOENCODING=utf-8 /usr/bin/python3 -m unittest discover -s tests -t . > /tmp/at17-old.log 2>&1; then
  echo "[OK]   历史提交 $HEAD_OLD（$NEWEST_TAG）测试通过: $(grep -E '^Ran |^OK' /tmp/at17-old.log | tr '\n' ' ')"
else
  echo "[FAIL] 历史提交 $HEAD_OLD 测试未通过（回滚目标不可用）"
  tail -20 /tmp/at17-old.log
fi

# 2.1b 再往回退一版：连续回滚两跳也必须可用（回滚不是只在相邻两版之间成立）
PREV_TAG=$(git tag --list 'contracts-v*' | sort -V | tail -2 | head -1)
if [ -n "$PREV_TAG" ] && [ "$PREV_TAG" != "$NEWEST_TAG" ]; then
  PREV=$(git rev-parse --verify "$PREV_TAG^{commit}" 2>/dev/null)
  git checkout -q "$PREV"
  if PYTHONUTF8=1 PYTHONIOENCODING=utf-8 /usr/bin/python3 -m unittest discover -s tests -t . > /tmp/at17-prev.log 2>&1; then
    echo "[OK]   再退一版 $PREV_TAG（$(git rev-parse --short HEAD)）测试通过: $(grep -E '^Ran |^OK' /tmp/at17-prev.log | tr '\n' ' ')"
  else
    echo "[FAIL] 再退一版 $PREV_TAG 测试未通过"
    tail -20 /tmp/at17-prev.log
  fi
fi

# 2.2 回到当前 main 再跑一次（回滚是双向的）
git checkout -q main
HEAD_NEW=$(git rev-parse --short HEAD)
if PYTHONUTF8=1 PYTHONIOENCODING=utf-8 /usr/bin/python3 -m unittest discover -s tests -t . > /tmp/at17-new.log 2>&1; then
  echo "[OK]   当前 main $HEAD_NEW 测试通过: $(grep -E '^Ran |^OK' /tmp/at17-new.log | tr '\n' ' ')"
else
  echo "[FAIL] 当前 main $HEAD_NEW 测试未通过"
  tail -20 /tmp/at17-new.log
fi

# 2.3 只读恢复：检出历史版本不得影响部署仓库
echo "部署仓库仍为: $(git -C "$HOME/code/asistent" rev-parse HEAD)"
echo "部署仓库 dirty 行数: $(git -C "$HOME/code/asistent" status --porcelain | wc -l)"
echo

echo "===== 3. 契约快照与版本一致性 ====="
cd "$HOME/code/asistent" || exit 1
grep -n 'CONTRACT_VERSION' asistent/contracts/version.py | head -3
ls contracts/
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 /usr/bin/python3 scripts/gen_contract_snapshot.py --check 2>&1 | tail -2

echo
echo "===== 4. 未授权下的网盘通道行为（只读探针） ====="
if [ -x "$HOME/.local/bin/bdpan" ]; then
  "$HOME/.local/bin/bdpan" --version 2>&1 | head -2
else
  echo "bdpan 不在 ~/.local/bin"
fi
cd "$DRILL" || exit 1
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 /usr/bin/python3 tests/archive/probe_real_bdpan_readonly.py 2>&1 | tail -12

echo
echo "DRILL-DONE"
