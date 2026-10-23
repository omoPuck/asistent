"""草稿版本化：`Draft` + `DraftVersion`（SPEC §5.5、§8.2、MVP-3 验收第 3 条）。

三条不可让渡的语义
==================

1. **每次修改产生新版本**，`DraftVersion` 记录作者/时间/内容哈希/依据引用/目标对象/修改原因
   （SPEC §5.5：「审批不能只绑定这个草稿，必须绑定确定的 DraftVersion」）；
2. **恢复旧版本 = 再产生一个新版本**，历史版本**永不覆盖**、永不删除
   （`restore_version` 只往 `version_ids` 追加）；
3. **审批只能绑定确定的 DraftVersion**：`:func:`approval_binding`` 返回
   `(draft_id, version_id, content_hash)`，并且会校验该版本确实属于该草稿、
   且仍是当前版本 —— 这是给 MVP-5/§8.4 的接口，本模块自己不审批、不发送。

版本 ID 是**内容决定的**（`digest_of([draft_id, version_no, body])`）：同一草稿同一版本号同一正文
重放会命中同一个对象，因此"重复处理同一封邮件"不会产生版本 1 的一份副本
（幂等口径见契约 §12.5）。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..contracts.enums import ActorKind, AuditAction, DraftState
from ..contracts.errors import NotFound, ValidationError
from ..contracts.ids import digest_of, new_id
from ..contracts.objects import Draft, DraftVersion
from ..contracts.store_api import StoreProtocol
from .model import CitationRef, DraftCandidate, hash_text

__all__ = [
    "DRAFT_ID_NAMESPACE",
    "VersionDiff",
    "derive_draft_id",
    "derive_version_id",
    "create_draft",
    "new_version",
    "diff_versions",
    "restore_version",
    "current_version",
    "version_by_number",
    "versions_of",
    "approval_binding",
]


#: 派生草稿 ID 时使用的命名空间（写进 `derive_*` 的种子，避免与其他 MVP 撞键）
DRAFT_ID_NAMESPACE = "mvp3-draft"


def _derive(object_type: str, *parts: Any) -> str:
    """确定性对象 ID：同一业务输入 → 同一 ID（重放时命中同一对象，SPEC §7.3）。"""
    return new_id(object_type, token=digest_of([DRAFT_ID_NAMESPACE, object_type, *parts])[:24])


def derive_draft_id(case_id: str, target_kind: str, target_ref: str) -> str:
    """草稿 ID：同一 Case + 同一目标 = 同一个草稿（多版本挂在它下面）。"""
    return _derive("Draft", case_id, target_kind, target_ref)


def derive_version_id(draft_id: str, version_no: int, body: str) -> str:
    """版本 ID：内容决定。同版本同内容重放命中同一对象。"""
    return _derive("DraftVersion", draft_id, int(version_no), hash_text(body))


def _citation_payloads(citations: Sequence[CitationRef]) -> List[Dict[str, Any]]:
    return [item.to_dict() for item in citations]


def _get(store: StoreProtocol, object_type: str, object_id: str) -> Optional[Any]:
    try:
        return store.get(object_type, object_id)
    except NotFound:
        return None


def _append_once(version_ids: Sequence[str], version_id: str) -> List[str]:
    """把版本 ID 追加到指针列表末尾；已在列表里就不重复追加（幂等重放/换存储后自愈）。"""
    out = [str(item) for item in version_ids]
    if version_id not in out:
        out.append(version_id)
    return out


def _draft_or_fail(store: StoreProtocol, draft_id: str) -> Draft:
    draft = _get(store, "Draft", draft_id)
    if draft is None:
        raise NotFound("草稿不存在: %s" % draft_id)
    return draft  # type: ignore[return-value]


def versions_of(store: StoreProtocol, draft_id: str) -> List[DraftVersion]:
    """按版本号升序返回该草稿的全部版本。

    权威来源是**存储里的 `DraftVersion` 对象**（按 `draft_id` 查），不是 `Draft.version_ids`
    这个反向指针：不同存储实现是否持久化 `version_ids` 并不一致，只读指针会在换存储之后
    让"重跑变成新建"（实测：SQLite 回来后 `version_ids` 为空 → 又建一版同样内容）。
    `version_ids` 仍然保留，用于审计与人工阅读。
    """
    _draft_or_fail(store, draft_id)
    out: List[DraftVersion] = [
        item
        for item in store.find("DraftVersion", where={"draft_id": draft_id}, limit=None)
        if str(item.draft_id) == str(draft_id)  # type: ignore[attr-defined]
    ]
    out.sort(key=lambda item: int(item.version_no))
    return out


def latest_version(store: StoreProtocol, draft_id: str) -> Optional[DraftVersion]:
    """版本号最大的那个版本（历史顺序的队尾）。"""
    history = versions_of(store, draft_id)
    return history[-1] if history else None


def version_by_number(store: StoreProtocol, draft_id: str, version_no: int) -> Optional[DraftVersion]:
    for version in versions_of(store, draft_id):
        if int(version.version_no) == int(version_no):
            return version
    return None


def current_version(store: StoreProtocol, draft_id: str) -> Optional[DraftVersion]:
    """当前版本：优先按 `Draft.current_version_id` 取，取不到就退化为"版本号最大"。"""
    draft = _draft_or_fail(store, draft_id)
    history = versions_of(store, draft_id)
    if draft.current_version_id:
        for version in history:
            if version.id == draft.current_version_id:
                return version
    return history[-1] if history else None


# --------------------------------------------------------------------------------------
# 创建与新增版本
# --------------------------------------------------------------------------------------


def create_draft(
    store: StoreProtocol,
    *,
    case_id: str,
    candidate: DraftCandidate,
    target_kind: str = "email_reply",
    target_ref: Optional[str] = None,
    actor_kind: str = ActorKind.AGENT,
    actor_id: str = "understand",
    reason: str = "",
    change_reason: str = "",
) -> Tuple[Draft, DraftVersion, bool]:
    """由草稿候选创建 `Draft` + 第 1 个 `DraftVersion`（状态 OPEN，未审批）。

    返回 `(draft, version, created)`：`created=False` 表示这次调用是**幂等重放**
    （草稿已存在、版本号已存在），没有产生任何新对象。

    幂等：草稿 ID 与版本 ID 都由内容派生，重复调用返回已有对象，不产生第二个草稿。
    """
    candidate.validate()
    resolved_ref = target_ref if target_ref is not None else candidate.target_ref
    draft_id = derive_draft_id(case_id, target_kind, resolved_ref)
    draft = _get(store, "Draft", draft_id)
    created_draft = False
    if draft is None:
        draft = store.put(  # type: ignore[assignment]
            Draft(
                id=draft_id,
                case_id=case_id,
                target_kind=target_kind,
                target_ref=resolved_ref,
                state=DraftState.OPEN,
                links={"case": [case_id]},
            ),
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason=reason or "为事务生成草稿容器",
            audit_action=str(AuditAction.CREATE),
            audit_detail={"case_id": case_id, "target_kind": target_kind, "target_ref": resolved_ref},
        )
        created_draft = True

    # 幂等判据：**存储里已存在的版本**，不是内存里的 `version_ids`（见 `versions_of` 的说明）。
    head = latest_version(store, draft_id)
    if head is not None and head.content_hash == candidate.content_hash():
        # 同内容重放：草稿与版本都已存在，本次没有产生任何新对象
        return _draft_or_fail(store, draft_id), head, False  # type: ignore[return-value]

    version_no = (int(head.version_no) + 1) if head is not None else 1
    version_id = derive_version_id(draft_id, version_no, candidate.body)
    existing_version = _get(store, "DraftVersion", version_id)
    if existing_version is not None:
        return _draft_or_fail(store, draft_id), existing_version, False  # type: ignore[return-value]

    version = store.put(  # type: ignore[assignment]
        DraftVersion(
            id=version_id,
            draft_id=draft_id,
            version_no=version_no,
            body=candidate.body,
            content_hash=candidate.content_hash(),
            author_kind=ActorKind.coerce(actor_kind) if isinstance(actor_kind, str) else actor_kind,
            author_id=actor_id,
            change_reason=change_reason or candidate.reason or "agent 初稿",
            target_object_id=None,
            summary=candidate.summary,
            citations=_citation_payloads(candidate.citations),
            links={"case": [case_id]},
        ),
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=change_reason or candidate.reason or "agent 初稿",
        audit_action=str(AuditAction.CREATE),
        audit_detail={"draft_id": draft_id, "version_no": version_no, "content_hash": hash_text(candidate.body)},
    )

    # 登记"当前版本"（`Draft` 记录本身在第一次创建之后不再动，只改指针字段）
    fresh = _draft_or_fail(store, draft_id)
    fresh.current_version_id = version.id
    fresh.version_ids = _append_once(fresh.version_ids, version.id)
    saved = store.put(  # type: ignore[assignment]
        fresh,
        expected_revision=fresh.revision,
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason="登记草稿首个版本" if created_draft else "登记草稿新版本",
        audit_action=str(AuditAction.UPDATE),
        audit_detail={"version_id": version.id, "version_no": version_no},
    )
    return saved, version, True  # type: ignore[return-value]


def new_version(
    store: StoreProtocol,
    draft_id: str,
    *,
    body: str,
    author_kind: str = ActorKind.USER,
    author_id: str = "user",
    change_reason: str = "",
    citations: Optional[Sequence[CitationRef]] = None,
    summary: str = "",
    target_object_id: Optional[str] = None,
    force: bool = False,
) -> DraftVersion:
    """在草稿上追加一个新版本（正文变化即会产生新版本，**不覆盖**任何历史）。

    幂等：若正文与**当前版本**的内容哈希相同，直接返回当前版本、不新增历史
    （否则"点两次保存"就会在历史里留下两条一模一样的记录，diff 也会变得没有意义）。

    `force=True` 强制产生新版本：**恢复旧版本**必须走这条路 —— 恢复的语义是
    「把内容改回 vN 并留下一条新历史」，不是"指针回退"（SPEC §5.5、§8.2）。
    """
    if not body or not body.strip():
        raise ValidationError("草稿版本正文不能为空")
    draft = _draft_or_fail(store, draft_id)
    new_hash = hash_text(body)
    head = current_version(store, draft_id)
    if not force and head is not None and head.content_hash == new_hash:
        return head  # type: ignore[return-value]
    # 版本号取自**存储里的实际历史**（不读 `version_ids`，见 `versions_of` 的说明）
    tail = latest_version(store, draft_id)
    version_no = (int(tail.version_no) + 1) if tail is not None else 1
    version_id = derive_version_id(draft_id, version_no, body)
    existing = _get(store, "DraftVersion", version_id)
    if existing is not None:
        return existing  # type: ignore[return-value]

    parent_id = head.id if head is not None else ""
    version = store.put(  # type: ignore[assignment]
        DraftVersion(
            id=version_id,
            draft_id=draft_id,
            version_no=version_no,
            body=body,
            content_hash=hash_text(body),
            author_kind=ActorKind.coerce(author_kind) if isinstance(author_kind, str) else author_kind,
            author_id=author_id,
            change_reason=change_reason or "编辑草稿",
            target_object_id=target_object_id,
            summary=summary,
            citations=_citation_payloads(citations or []),
            parent_version_id=parent_id,
            links={"case": [draft.case_id]} if draft.case_id else {},
        ),
        actor_kind=author_kind,
        actor_id=author_id,
        reason=change_reason or "编辑草稿",
        audit_action=str(AuditAction.CREATE),
        audit_detail={
            "draft_id": draft_id,
            "version_no": version_no,
            "parent_version_id": parent_id,
            "content_hash": hash_text(body),
        },
    )
    fresh = _draft_or_fail(store, draft_id)
    fresh.current_version_id = version.id
    fresh.version_ids = _append_once(fresh.version_ids, version.id)
    if str(fresh.state) == str(DraftState.LOCKED):
        # 编辑后不再"锁定"：锁定是"当前版本已定稿"，改内容必然产生新版本
        fresh.state = DraftState.STALE
    store.put(
        fresh,
        expected_revision=fresh.revision,
        actor_kind=author_kind,
        actor_id=author_id,
        reason="草稿产生新版本（旧版本保留）",
        audit_action=str(AuditAction.UPDATE),
        audit_detail={"version_id": version.id, "version_no": version_no},
    )
    return version  # type: ignore[return-value]


# --------------------------------------------------------------------------------------
# 差异与恢复
# --------------------------------------------------------------------------------------


@dataclass
class VersionDiff:
    """两个版本之间的差异（可展示，也可机读）。"""

    draft_id: str
    from_version_no: int
    to_version_no: int
    from_version_id: str
    to_version_id: str
    unified: str
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)
    unchanged_lines: int = 0
    identical: bool = False
    from_content_hash: str = ""
    to_content_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "from_version_no": self.from_version_no,
            "to_version_no": self.to_version_no,
            "from_version_id": self.from_version_id,
            "to_version_id": self.to_version_id,
            "unified": self.unified,
            "added_lines": list(self.added_lines),
            "removed_lines": list(self.removed_lines),
            "unchanged_lines": self.unchanged_lines,
            "identical": self.identical,
            "from_content_hash": self.from_content_hash,
            "to_content_hash": self.to_content_hash,
        }


def diff_versions(
    store: StoreProtocol,
    draft_id: str,
    from_version_no: int,
    to_version_no: int,
) -> VersionDiff:
    """查看**任意两版**的差异（SPEC §8.2「修改前后差异查看」）。

    逐行 diff（`difflib`），同时给出统一的 unified diff 文本与增删行列表，
    便于手机端展示与审计核查。
    """
    left = version_by_number(store, draft_id, from_version_no)
    right = version_by_number(store, draft_id, to_version_no)
    if left is None or right is None:
        raise NotFound(
            "版本不存在: draft=%s from=v%s to=v%s" % (draft_id, from_version_no, to_version_no)
        )
    left_lines = left.body.splitlines()
    right_lines = right.body.splitlines()
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines, autojunk=False)
    added: List[str] = []
    removed: List[str] = []
    unchanged = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            unchanged += i2 - i1
        elif tag == "delete":
            removed.extend(left_lines[i1:i2])
        elif tag == "insert":
            added.extend(right_lines[j1:j2])
        else:  # replace
            removed.extend(left_lines[i1:i2])
            added.extend(right_lines[j1:j2])
    unified = "\n".join(
        difflib.unified_diff(
            left_lines,
            right_lines,
            fromfile="v%d" % int(left.version_no),
            tofile="v%d" % int(right.version_no),
            lineterm="",
        )
    )
    return VersionDiff(
        draft_id=draft_id,
        from_version_no=int(left.version_no),
        to_version_no=int(right.version_no),
        from_version_id=left.id,
        to_version_id=right.id,
        unified=unified,
        added_lines=added,
        removed_lines=removed,
        unchanged_lines=unchanged,
        identical=left.content_hash == right.content_hash,
        from_content_hash=left.content_hash,
        to_content_hash=right.content_hash,
    )


def restore_version(
    store: StoreProtocol,
    draft_id: str,
    version_no: int,
    *,
    author_kind: str = ActorKind.USER,
    author_id: str = "user",
    reason: str = "",
) -> DraftVersion:
    """恢复旧版本：**恢复也产生新版本**（SPEC §5.5、§8.2 的"恢复旧版本不覆盖历史"）。

    恢复出的版本：
    - 正文与被恢复的版本一致（`content_hash` 相同，便于核对）；
    - `change_reason` 写明"恢复自 vN"；
    - `parent_version_id` 指向**恢复时的当前版本**，历史链因此保持可追溯。
    """
    source = version_by_number(store, draft_id, version_no)
    if source is None:
        raise NotFound("要恢复的版本不存在: draft=%s v%s" % (draft_id, version_no))
    return new_version(
        store,
        draft_id,
        body=source.body,
        author_kind=author_kind,
        author_id=author_id,
        change_reason=reason or ("恢复自 v%d" % int(source.version_no)),
        summary="恢复自 v%d" % int(source.version_no),
        # 恢复必须留下新历史：即使正文与当前版本逐字相同（"就是要改回那个说法"）
        force=True,
    )


# --------------------------------------------------------------------------------------
# 审批绑定（给 MVP-5 / §8.4 用的接口）
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalBinding:
    """审批必须绑定的三元组：草稿、**确定版本**、内容哈希。"""

    draft_id: str
    version_id: str
    version_no: int
    content_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "version_id": self.version_id,
            "version_no": self.version_no,
            "content_hash": self.content_hash,
        }


def approval_binding(store: StoreProtocol, draft_id: str, version_no: Optional[int] = None) -> ApprovalBinding:
    """返回可审批的确定版本；不给版本号时取当前版本。

    - 草稿没有版本 → `ValidationError`（不能审批"一个还没有内容的草稿"）；
    - 指定版本不是当前版本 → `ValidationError`（审批"历史版本"会让内容与审批脱钩）。
    """
    draft = _draft_or_fail(store, draft_id)
    if not versions_of(store, draft_id):
        raise ValidationError("草稿还没有任何版本，不能审批: %s" % draft_id)
    current = current_version(store, draft_id)
    if version_no is None:
        target = current
    else:
        target = version_by_number(store, draft_id, version_no)
        if target is None:
            raise NotFound("版本不存在: draft=%s v%s" % (draft_id, version_no))
        if current is None or target.id != current.id:
            raise ValidationError(
                "只能审批当前版本（历史版本已不是草稿内容）: draft=%s 指定 v%s，当前 v%s"
                % (draft_id, version_no, _current_no(store, draft_id))
            )
    if target is None:  # pragma: no cover - 由"至少一个版本"的不变量保证
        raise ValidationError("草稿当前版本缺失: %s" % draft_id)
    return ApprovalBinding(
        draft_id=draft.id,
        version_id=target.id,
        version_no=int(target.version_no),
        content_hash=target.content_hash,
    )


def _current_no(store: StoreProtocol, draft_id: str) -> int:
    version = current_version(store, draft_id)
    return int(version.version_no) if version is not None else 0
