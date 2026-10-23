/* 事务台 · 手机控制台前端
 *
 * 约束（不要违反）：
 *  - 无第三方依赖、无构建步骤：直接跑在手机浏览器里；
 *  - 令牌只放内存变量 `state.token`，**不进 URL、不落浏览器持久存储**；
 *  - 旧待办不靠内存列表维护：重连时用 `next_since` 增量 + 每次返回的完整待处理快照；
 *  - 版本冲突（409）必须显示服务端给的原因，并自动刷新到最新版本，绝不静默覆盖。
 */
'use strict';

var state = {
  token: '',
  connected: false,
  since: '',
  etag: '',
  pending: null,
  caseId: '',
  tab: 'pending'
};

function $(id) { return document.getElementById(id); }

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined && text !== null) { node.textContent = String(text); }
  return node;
}

function setConn(stateName, text) {
  var node = $('conn');
  node.dataset.state = stateName;
  node.textContent = text;
}

var toastTimer = null;
function toast(message) {
  var node = $('toast');
  node.textContent = message;
  node.hidden = false;
  if (toastTimer) { clearTimeout(toastTimer); }
  toastTimer = setTimeout(function () { node.hidden = true; }, 3200);
}

/* ------------------------------------------------------------------ HTTP */

function request(method, path, body, opts) {
  opts = opts || {};
  var headers = { 'Accept': 'application/json', 'X-Asistent-Client': 'console' };
  if (state.token) { headers['Authorization'] = 'Bearer ' + state.token; }
  if (opts.etag) { headers['If-None-Match'] = opts.etag; }
  var payload = null;
  if (body !== undefined && body !== null) {
    payload = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  }
  setConn('busy', '同步中');
  return fetch(path, {
    method: method,
    headers: headers,
    body: payload,
    cache: 'no-store',
    credentials: 'same-origin'  // 会话 Cookie 必须随请求发送（HttpOnly，JS 读不到）
  }).then(function (response) {
    if (response.status === 304) {
      setConn('on', '已连接');
      return { status: 304, data: null };
    }
    return response.text().then(function (text) {
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (err) { data = null; }
      if (response.ok) {
        setConn('on', '已连接');
        return { status: response.status, data: data, headers: response.headers };
      }
      var error = new Error(
        (data && data.error && data.error.message) || ('HTTP ' + response.status)
      );
      error.status = response.status;
      error.code = data && data.error ? data.error.code : '';
      error.detail = data && data.error ? data.error.detail : null;
      setConn(response.status === 401 ? 'off' : 'busy', response.status === 401 ? '未授权' : '出错');
      throw error;
    });
  }).catch(function (err) {
    if (!err.status) {
      setConn('off', '离线');
      err.offline = true;
    }
    throw err;
  });
}

function get(path) { return request('GET', path, null).then(function (r) { return r.data; }); }
function post(path, body) { return request('POST', path, body).then(function (r) { return r.data; }); }

/* ------------------------------------------------------------------ 登录 */

function connect() {
  var token = $('token').value.trim();
  var box = $('gate-error');
  box.hidden = true;
  if (!token) { box.textContent = '请先粘贴令牌'; box.hidden = false; return; }
  state.token = token;
  request('GET', '/api/overview', null).then(function (result) {
    if (result.status === 304) { return null; }
    applyOverview(result.data, { reset: true });
    // 换会话：之后浏览器自动带 HttpOnly Cookie，令牌不再需要留在内存里
    return request('POST', '/api/session', {}).then(function () {
      state.token = '';
      state.connected = true;
    }).catch(function () {
      // 会话换不到不算致命：本页仍可用内存令牌继续操作
      state.connected = true;
    });
  }).then(function () {
    $('gate').hidden = true;
    $('app').hidden = false;
    switchTab('pending');
    refreshAll();
  }).catch(function (err) {
    state.token = '';
    state.connected = false;
    box.textContent = err.status === 401
      ? '令牌不正确（服务端不对未授权请求透露任何信息）'
      : ('连接失败：' + err.message);
    box.hidden = false;
  });
}

/* ------------------------------------------------------------------ 待办 */

function applyOverview(data, options) {
  options = options || {};
  if (options.reset) {
    state.pending = data.pending;
    state.since = data.next_since || '';
    state.etag = data.etag || '';
  } else if (data.pending) {
    // 服务端每次都回完整待处理快照：断网期间的新增不会漏
    state.pending = data.pending;
    state.since = data.next_since || state.since;
    state.etag = data.etag || state.etag;
  }
  renderPending();
}

function refreshAll() {
  var options = { etag: state.etag };
  return request('GET', '/api/overview', null, options).then(function (result) {
    if (result.status === 304) { return; }
    applyOverview(result.data, { reset: false });
  }).catch(function (err) {
    if (err.offline) { toast('离线：显示的是上一次的待办，重连后自动补齐'); }
  });
}

function refreshIncremental() {
  if (!state.since) { return refreshAll(); }
  return get('/api/changes?since=' + encodeURIComponent(state.since)).then(function (data) {
    applyOverview(data, { reset: false });
  }).catch(function () { /* 保持旧视图 */ });
}

function renderPending() {
  var pending = state.pending || { counts: {}, case_ids: [], question_ids: [], approval_ids: [], draft_ids: [], artifact_ids: [] };
  var counts = pending.counts || {};
  var summary = $('pending-summary');
  summary.innerHTML = '';
  var labels = [
    ['cases', '事务'], ['questions', '追问'], ['approvals', '待审批'],
    ['drafts', '草稿'], ['tasks', '任务'], ['failed_transfers', '传输失败']
  ];
  labels.forEach(function (pair) {
    if (!counts[pair[0]]) { return; }
    var pill = el('span', 'pill' + (pair[0] === 'questions' || pair[0] === 'approvals' ? ' wait' : ''), pair[1] + ' ' + counts[pair[0]]);
    summary.appendChild(pill);
  });
  var badge = $('badge-pending');
  var total = (counts.questions || 0) + (counts.approvals || 0);
  badge.textContent = total ? String(total) : '';

  var list = $('pending-list');
  list.innerHTML = '';
  var questions = pending.question_ids || [];
  var approvals = pending.approval_ids || [];
  var drafts = pending.draft_ids || [];
  var files = pending.artifact_ids || [];
  $('pending-empty').hidden = !!(questions.length || approvals.length || drafts.length || files.length || (pending.case_ids || []).length);

  questions.forEach(function (id, index) { list.appendChild(renderPendingQuestion(id, index === 0)); });
  approvals.forEach(function (id) { list.appendChild(renderPendingApproval(id)); });
  drafts.forEach(function (id) { list.appendChild(renderPendingDraft(id)); });
  files.forEach(function (id) { list.appendChild(renderArtifactRow(id)); });
}

function renderPendingQuestion(questionId) {
  var item = el('li');
  var row = el('div', 'row');
  row.appendChild(el('strong', null, '待回答追问'));
  row.appendChild(el('span', 'obj-id', questionId));
  item.appendChild(row);
  item.appendChild(el('p', 'hint', '加载中…'));
  get('/api/questions/' + encodeURIComponent(questionId)).then(function (data) {
    item.innerHTML = '';
    var head = el('div', 'row');
    head.appendChild(el('strong', null, '待回答追问'));
    head.appendChild(el('span', 'obj-id', data.case_id));
    item.appendChild(head);
    item.appendChild(el('div', 'quote', data.question));
    if (data.background) { item.appendChild(el('p', 'hint', '背景：' + data.background)); }
    item.appendChild(el('p', 'hint', '要求格式：' + (data.required_format || 'text')));

    var field = el('label', 'field');
    field.appendChild(el('span', null, '你的回答（revision ' + data.revision + '）'));
    var input = el('input');
    input.type = 'text';
    input.placeholder = '一句话回答即可';
    field.appendChild(input);
    item.appendChild(field);

    var actions = el('div', 'actions');
    var submit = el('button', 'primary', '提交回答');
    submit.addEventListener('click', function () {
      submit.disabled = true;
      post('/api/questions/' + encodeURIComponent(questionId) + '/answer', {
        answer_text: input.value,
        expected_revision: data.revision
      }).then(function (result) {
        toast('回答已提交；依赖任务重新评估：' + describeReevaluation(result.reevaluated_tasks));
        refreshAll();
        if (state.caseId === result.question.case_id) { openCase(state.caseId); }
      }).catch(function (err) { showConflict(err, item); }).then(function () { submit.disabled = false; });
    });
    actions.appendChild(submit);
    item.appendChild(actions);
  }).catch(function (err) { item.appendChild(renderError(err)); });
  return item;
}

function describeReevaluation(rows) {
  if (!rows || !rows.length) { return '无依赖任务'; }
  var changed = rows.filter(function (row) { return row.changed; });
  return changed.length ? changed.map(function (row) { return row.task_id + ' ' + row.from + '→' + row.to; }).join('，') : '依赖任务状态无需变化';
}

function renderPendingApproval(approvalId) {
  var item = el('li');
  get('/api/approvals/' + encodeURIComponent(approvalId)).then(function (data) {
    var head = el('div', 'row');
    head.appendChild(el('strong', null, '待审批'));
    head.appendChild(el('span', 'pill wait', data.state));
    item.appendChild(head);
    item.appendChild(el('p', 'hint', '绑定版本：' + (data.target_object_ids || []).join(', ')));
    var open = el('button', 'ghost', '打开所在事务');
    open.addEventListener('click', function () { openCase(data.case_id); });
    item.appendChild(open);
  }).catch(function (err) { item.appendChild(renderError(err)); });
  return item;
}

function renderPendingDraft(draftId) {
  var item = el('li');
  item.appendChild(el('p', 'hint', '加载中…'));
  get('/api/drafts/' + encodeURIComponent(draftId)).then(function (data) {
    item.innerHTML = '';
    var head = el('div', 'row');
    head.appendChild(el('strong', null, '可编辑草稿'));
    head.appendChild(el('span', 'obj-id', data.id));
    item.appendChild(head);
    var current = data.current || {};
    item.appendChild(el('p', 'hint', '当前 v' + (current.version_no || '?') + ' · 共 ' + (data.versions || []).length + ' 个版本'));
    var actions = el('div', 'actions');
    var open = el('button', null, '打开');
    open.addEventListener('click', function () { openCase(data.case_id); });
    actions.appendChild(open);
    item.appendChild(actions);
  }).catch(function (err) { item.appendChild(renderError(err)); });
  return item;
}

function renderError(err) {
  var box = el('pre', 'error', err.message + (err.detail ? '\n' + JSON.stringify(err.detail, null, 2) : ''));
  return box;
}

/* ------------------------------------------------------------------ 事务 */

function loadCases() {
  var include = $('show-closed').checked ? '&include_closed=true' : '';
  return get('/api/cases?limit=50' + include).then(function (data) {
    var list = $('case-list');
    list.innerHTML = '';
    (data.cases || []).forEach(function (row) {
      var item = el('li');
      var head = el('div', 'row');
      head.appendChild(el('strong', null, row.goal || row.id));
      head.appendChild(el('span', 'pill ' + stateClass(row.state), row.state));
      item.appendChild(head);
      item.appendChild(el('p', 'hint', '等待原因：' + (row.waiting_reason || '—') + ' · 下一步：' + (row.next_action || '—')));
      if (row.last_action_summary) { item.appendChild(el('p', 'hint', '最近动作：' + row.last_action_summary)); }
      item.appendChild(el('p', 'obj-id', row.id));
      var open = el('button', 'ghost', '查看详情');
      open.addEventListener('click', function () { openCase(row.id); });
      item.appendChild(open);
      list.appendChild(item);
    });
    if (!(data.cases || []).length) { list.appendChild(el('li', 'hint', '暂无事务')); }
  });
}

function stateClass(caseState) {
  if (caseState === 'BLOCKED' || caseState === 'FAILED' || caseState === 'UNKNOWN') { return 'bad'; }
  if (caseState === 'WAITING_FOR_USER' || caseState === 'WAITING_FOR_CONTEXT') { return 'wait'; }
  if (caseState === 'COMPLETED' || caseState === 'SUCCEEDED') { return 'ok'; }
  return '';
}

function openCase(caseId) {
  state.caseId = caseId;
  switchTab('cases');
  return get('/api/cases/' + encodeURIComponent(caseId) + '?include=audit,documents').then(function (data) {
    renderCase(data);
  }).catch(function (err) { toast('打不开事务：' + err.message); });
}

function renderCase(data) {
  $('case-detail').hidden = false;
  $('case-title').textContent = data.goal || data.id;
  var fields = $('case-fields');
  fields.innerHTML = '';
  var rows = [
    ['事务 ID', data.id], ['状态', data.state], ['revision', data.revision],
    ['等待原因', data.waiting_reason || '—'], ['阻塞原因', data.blocked_reason || '—'],
    ['下一步', data.next_action || '—'], ['最近动作', data.last_action_summary || '—'],
    ['结果', data.result_summary || '—'], ['失败分类', data.failure_class || '—'],
    ['归档状态', data.archive_state], ['更新时间', data.updated_at]
  ];
  rows.forEach(function (pair) {
    fields.appendChild(el('dt', null, pair[0]));
    fields.appendChild(el('dd', null, pair[1]));
  });

  var agent = $('case-agent');
  agent.innerHTML = '';
  agent.appendChild(el('h3', null, 'Agent 状态'));
  var agentRow = data.agent || { running: false, recent_executions: [], running_tasks: [] };
  agent.appendChild(el('p', 'hint', agentRow.running ? '正在执行（' + agentRow.current_execution_id + '）' : '当前无执行在跑'));
  (agentRow.recent_executions || []).slice().reverse().forEach(function (exec) {
    var box = el('div', 'quote', [exec.state, exec.backend, exec.model || ''].join(' · ') + '\n' + (exec.output_summary || exec.input_summary || ''));
    agent.appendChild(box);
  });

  var qbox = $('case-questions');
  qbox.innerHTML = '';
  qbox.appendChild(el('h3', null, '追问'));
  (data.questions || []).forEach(function (question) {
    var box = el('div', 'sub');
    box.appendChild(el('span', 'pill ' + (question.state === 'OPEN' ? 'wait' : 'ok'), question.state));
    box.appendChild(el('div', 'quote', question.question));
    if (question.state === 'OPEN') {
      var field = el('label', 'field');
      field.appendChild(el('span', null, '回答（revision ' + question.revision + '）'));
      var input = el('input');
      field.appendChild(input);
      box.appendChild(field);
      var actions = el('div', 'actions');
      var submit = el('button', 'primary', '提交回答');
      submit.addEventListener('click', function () {
        submit.disabled = true;
        post('/api/questions/' + encodeURIComponent(question.id) + '/answer', {
          answer_text: input.value, expected_revision: question.revision
        }).then(function (result) {
          toast('回答已提交：' + describeReevaluation(result.reevaluated_tasks));
          refreshAll(); renderCase(result.case);
        }).catch(function (err) { box.appendChild(renderError(err)); })
          .then(function () { submit.disabled = false; });
      });
      actions.appendChild(submit);
      box.appendChild(actions);
    } else if (question.answer_id) {
      box.appendChild(el('p', 'hint', '已回答：' + question.answer_id));
    }
    qbox.appendChild(box);
  });

  var dbox = $('case-drafts');
  dbox.innerHTML = '';
  dbox.appendChild(el('h3', null, '草稿版本'));
  (data.drafts || []).forEach(function (draft) { dbox.appendChild(renderDraftBox(draft)); });

  var abox = $('case-approvals');
  abox.innerHTML = '';
  abox.appendChild(el('h3', null, '审批'));
  (data.approvals || []).forEach(function (approval) {
    var box = el('div', 'sub');
    box.appendChild(el('p', null, approval.id + ' · ' + approval.state + ' · 绑定 ' + (approval.target_object_ids || []).join(',')));
    if (approval.invalidated_reason) { box.appendChild(el('p', 'hint', '失效原因：' + approval.invalidated_reason)); }
    if (approval.state === 'ACTIVE') {
      var revoke = el('button', 'danger', '撤销审批');
      revoke.addEventListener('click', function () {
        revoke.disabled = true;
        post('/api/approvals/' + encodeURIComponent(approval.id) + '/revoke', {
          reason: '用户在手机端撤销', expected_revision: approval.revision
        }).then(function () { toast('已撤销'); refreshAll(); openCase(data.id); })
          .catch(function (err) { box.appendChild(renderError(err)); })
          .then(function () { revoke.disabled = false; });
      });
      box.appendChild(revoke);
    }
    abox.appendChild(box);
  });

  var fbox = $('case-facts');
  fbox.innerHTML = '';
  fbox.appendChild(el('h3', null, '事实与引用'));
  (data.facts || []).forEach(function (fact) {
    var box = el('div', 'sub');
    box.appendChild(el('p', null, fact.statement));
    var cites = el('ul', 'cites');
    (fact.citations || []).forEach(function (cite) {
      cites.appendChild(el('li', null, cite.path + ' @ ' + cite.locator + ' → ' + (cite.supports || '')));
    });
    if (cites.children.length) { box.appendChild(cites); }
    fbox.appendChild(box);
  });
  (data.documents || []).forEach(function (document) {
    fbox.appendChild(el('p', 'hint', '原始对象：' + document.id + ' · ' + document.source_type + ' · content_hash ' + String(document.content_hash || '').slice(0, 12)));
  });

  var audit = $('case-audit');
  audit.innerHTML = '';
  (data.audit || []).forEach(function (entry) {
    audit.appendChild(el('li', null, entry.at + ' · ' + entry.action + ' · ' + entry.actor_id + ' · ' + (entry.reason || '')));
  });
}

function renderDraftBox(draft) {
  var box = el('div', 'sub');
  box.appendChild(el('p', null, draft.id + ' · state=' + draft.state + ' · revision=' + draft.revision));
  var holder = el('div');
  holder.appendChild(el('p', 'hint', '加载版本…'));
  box.appendChild(holder);
  get('/api/drafts/' + encodeURIComponent(draft.id)).then(function (data) {
    holder.innerHTML = '';
    var versions = data.versions || [];
    var current = data.current || {};
    holder.appendChild(el('p', 'hint', versions.length + ' 个版本，当前 v' + (current.version_no || '?')));
    var body = (current.body || '');
    var preview = el('div', 'preview', body || '（空）');
    holder.appendChild(preview);

    var field = el('label', 'field');
    field.appendChild(el('span', null, '编辑正文（基于 v' + (current.version_no || '?') + '，后端会生成新版本）'));
    var textarea = el('textarea');
    textarea.value = body;
    field.appendChild(textarea);
    holder.appendChild(field);

    var reasonField = el('label', 'field');
    reasonField.appendChild(el('span', null, '修改原因'));
    var reasonInput = el('input');
    reasonInput.value = '';
    reasonInput.placeholder = '例如：改一下时间';
    reasonField.appendChild(reasonInput);
    holder.appendChild(reasonField);

    var actions = el('div', 'actions');
    var save = el('button', 'primary', '保存为新版本');
    save.addEventListener('click', function () {
      save.disabled = true;
      post('/api/drafts/' + encodeURIComponent(draft.id) + '/edit', {
        body: textarea.value,
        change_reason: reasonInput.value,
        version_no: current.version_no,
        expected_revision: draft.revision
      }).then(function (result) {
        var invalidated = result.invalidated_approval_ids || [];
        toast('已保存 v' + result.created_version.version_no + (invalidated.length ? ('；旧审批失效：' + invalidated.join(',')) : ''));
        openCase(draft.case_id);
      }).catch(function (err) { holder.appendChild(renderError(err)); })
        .then(function () { save.disabled = false; });
    });
    actions.appendChild(save);

    if (versions.length >= 2) {
      var diffButton = el('button', null, '比较 v' + versions[versions.length - 2].version_no + ' → v' + versions[versions.length - 1].version_no);
      var diffHolder = el('div');
      diffButton.addEventListener('click', function () {
        get('/api/drafts/' + encodeURIComponent(draft.id) + '/diff?from=' + versions[versions.length - 2].version_no +
            '&to=' + versions[versions.length - 1].version_no).then(function (delta) {
          diffHolder.innerHTML = '';
          var pre = el('pre', 'diff');
          (delta.unified || '').split('\n').forEach(function (line) {
            var span = el('span', line.charAt(0) === '+' ? 'add' : (line.charAt(0) === '-' ? 'del' : ''), line + '\n');
            pre.appendChild(span);
          });
          diffHolder.appendChild(pre);
        });
      });
      actions.appendChild(diffButton);
      holder.appendChild(actions);
      holder.appendChild(diffHolder);
    } else {
      holder.appendChild(actions);
    }

    // 审批：只有这里、且必须显式确认 + 绑定版本号
    var approveBox = el('div', 'actions');
    var approve = el('button', 'danger', '批准 v' + (current.version_no || '?') + '（显式确认）');
    approve.addEventListener('click', function () {
      approve.disabled = true;
      post('/api/approvals', {
        draft_id: draft.id,
        version_no: current.version_no,
        confirm: true,
        action_digest: current.content_hash
      }).then(function (result) {
        toast('已批准：' + result.approval.id);
        openCase(draft.case_id);
      }).catch(function (err) { holder.appendChild(renderError(err)); })
        .then(function () { approve.disabled = false; });
    });
    approveBox.appendChild(approve);
    holder.appendChild(approveBox);

    versions.slice().reverse().forEach(function (version) {
      holder.appendChild(el('p', 'hint', 'v' + version.version_no + ' · ' + version.author_kind + '/' + version.author_id +
        ' · ' + version.created_at + ' · ' + String(version.content_hash || '').slice(0, 12) + ' · ' + (version.change_reason || '')));
    });
  }).catch(function (err) { holder.appendChild(renderError(err)); });
  return box;
}

/* ------------------------------------------------------------------ 文件 */

function loadFiles() {
  return get('/api/artifacts?limit=100').then(function (data) {
    var list = $('file-list');
    list.innerHTML = '';
    (data.artifacts || []).forEach(function (artifact) { list.appendChild(renderArtifactRow(artifact.id)); });
    if (!(data.artifacts || []).length) { list.appendChild(el('li', 'hint', '暂无生成文件')); }
  });
}

function renderArtifactRow(artifactId) {
  var item = el('li');
  item.appendChild(el('p', 'hint', '加载中…'));
  get('/api/artifacts/' + encodeURIComponent(artifactId)).then(function (data) {
    item.innerHTML = '';
    var head = el('div', 'row');
    head.appendChild(el('strong', null, data.name || data.id));
    head.appendChild(el('span', 'pill' + (data.transfer_failures ? ' bad' : ''), data.content_type));
    item.appendChild(head);
    item.appendChild(el('p', 'hint', data.size_bytes + ' 字节 · sha256 ' + String(data.sha256 || '').slice(0, 16) +
      ' · 下载 ' + data.download_count + ' 次 · 失败 ' + data.transfer_failures + ' 次'));
    if (data.transfer_failures) { item.appendChild(el('p', 'hint', '上次传输失败，可重试')); }
    var actions = el('div', 'actions');
    var download = el('button', null, '下载');
    var output = el('pre', 'error');
    output.hidden = true;
    download.addEventListener('click', function () {
      download.disabled = true;
      output.hidden = true;
      fetch('/api/artifacts/' + encodeURIComponent(artifactId) + '/content', {
        headers: { 'Authorization': 'Bearer ' + state.token }
      }).then(function (response) {
        if (!response.ok) {
          return response.text().then(function (text) {
            var parsed = null;
            try { parsed = JSON.parse(text); } catch (err) { parsed = null; }
            throw new Error((parsed && parsed.error && parsed.error.message) || ('HTTP ' + response.status));
          });
        }
        return response.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var link = document.createElement('a');
          link.href = url;
          link.download = data.name || 'artifact';
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
          toast('已开始下载 ' + (data.name || artifactId));
        });
      }).catch(function (err) {
        output.textContent = '下载失败：' + err.message + '（可重试）';
        output.hidden = false;
      }).then(function () { download.disabled = false; });
    });
    actions.appendChild(download);
    item.appendChild(actions);
    item.appendChild(output);
    // 待办区里也展示同一个渲染：直接把审批行替换掉
  }).catch(function (err) { item.appendChild(renderError(err)); });
  return item;
}

/* ------------------------------------------------------------------ 冲突处理 */

function showConflict(err, container) {
  if (err.status === 409) {
    toast('版本已变，已刷新到最新状态');
    container.appendChild(renderError(err));
    refreshAll();
    return;
  }
  container.appendChild(renderError(err));
  if (err.status === 401) { lock(); }
}

function lock() {
  // 主动退出：先把服务端会话吊销，再清本地状态（否则 Cookie 还在，刷新又进去了）
  request('DELETE', '/api/session', null).catch(function () { /* 已失效也无所谓 */ });
  state.token = '';
  state.connected = false;
  state.pending = null;
  $('app').hidden = true;
  $('gate').hidden = false;
  setConn('off', '未授权');
  toast('令牌失效，请重新输入');
}

/* ------------------------------------------------------------------ 启动 */

function switchTab(name) {
  state.tab = name;
  ['pending', 'cases', 'files'].forEach(function (key) {
    $('tab-' + key).hidden = key !== name;
  });
  Array.prototype.forEach.call($('tabs').children, function (button) {
    var on = button.dataset.tab === name;
    if (on) { button.classList.add('active'); } else { button.classList.remove('active'); }
    // role=tab 必须同步 aria-selected，否则读屏软件会一直念"已选中"
    button.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if (name === 'cases') { loadCases(); }
  if (name === 'files') { loadFiles(); }
}

$('connect').addEventListener('click', connect);
$('token').addEventListener('keydown', function (event) { if (event.key === 'Enter') { connect(); } });
$('refresh').addEventListener('click', function () { refreshIncremental().then(function () { toast('已同步'); }); });
$('show-closed').addEventListener('change', loadCases);
$('tabs').addEventListener('click', function (event) {
  var button = event.target.closest('button[data-tab]');
  if (button) { switchTab(button.dataset.tab); }
});

// 断线重连：恢复可见时增量拉一次（不需要用户手动刷新）
document.addEventListener('visibilitychange', function () {
  if (!document.hidden && state.connected) { refreshAll(); }
});
window.addEventListener('online', function () { if (state.connected) { refreshAll(); } });
window.addEventListener('offline', function () { setConn('off', '离线'); });

// 启动：先试会话 Cookie（PWA 从桌面打开时通常直接进主界面，不用再粘令牌）。
// 没有有效会话时服务端返回 401，此时保持登录页——这正是"未授权不泄露任何信息"。
function bootWithSession() {
  request('GET', '/api/overview', null).then(function (result) {
    if (result.status === 304) { return; }
    applyOverview(result.data, { reset: true });
    state.connected = true;
    $('gate').hidden = true;
    $('app').hidden = false;
    switchTab('pending');
    refreshAll();
  }).catch(function () {
    // 没有会话是正常情况（第一次用/会话过期），徽标说"未连接"而不是"未授权"
    setConn('off', '未连接');
  });
}

if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () { /* 非 HTTPS 环境忽略 */ });
  });
}
bootWithSession();
