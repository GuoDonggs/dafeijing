/* 大肥鲸 控制台前端 —— 无框架、无构建，只用原生 DOM 与 fetch */
(function () {
  'use strict';

  var TOKEN = window.VOICE_TOKEN || '';
  var LEVELS = [];
  var LAST_TRANSCRIPT = [];
  var LAST_TRANSCRIPT_OLD = [];
  var RENDERED = 0;
  var SKILL_DIRTY = false;
  var SKILL_EDITING = '';

  function $(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function api(path, body) {
    var options = { headers: { 'X-Voice-Token': TOKEN } };
    if (body) {
      body.token = TOKEN;
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, error: '服务返回了非 JSON 内容（HTTP ' + response.status + '）' };
      });
    });
  }

  function toast(text, kind) {
    var box = document.createElement('div');
    box.className = 'toast ' + (kind || '');
    box.textContent = text;
    $('toasts').appendChild(box);
    setTimeout(function () { box.remove(); }, kind === 'err' ? 6500 : 3200);
  }

  /* ───────────── 主题 ───────────── */

  function applyTheme(name) {
    document.documentElement.dataset.theme = name;
    try { localStorage.setItem('voice-theme', name); } catch (err) { /* 隐私模式下忽略 */ }
    drawWave(LEVELS[LEVELS.length - 1] || 0);
  }

  function initTheme() {
    var saved = '';
    try { saved = localStorage.getItem('voice-theme') || ''; } catch (err) { saved = ''; }
    if (!saved) {
      saved = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    applyTheme(saved);
  }

  /* ───────────── 电平波形 ───────────── */

  function drawWave(level) {
    var canvas = $('wave');
    if (!canvas || !canvas.getContext) { return; }
    var ctx = canvas.getContext('2d');
    var width = canvas.width;
    var height = canvas.height;
    var styles = getComputedStyle(document.documentElement);
    var accent = getComputedStyle(document.body).getPropertyValue('--accent').trim() || '#38bdf8';
    var muted = styles.getPropertyValue('--border').trim() || 'rgba(255,255,255,0.1)';
    ctx.clearRect(0, 0, width, height);

    ctx.fillStyle = muted;
    ctx.fillRect(0, height / 2 - 0.5, width, 1);

    var count = LEVELS.length;
    if (!count) { return; }
    var barWidth = Math.max(2, Math.floor(width / 56) - 2);
    var gap = Math.max(1, Math.floor((width - count * barWidth) / Math.max(1, count - 1)));
    var x = width - count * (barWidth + gap);
    for (var i = 0; i < count; i++) {
      var value = Math.min(1, (LEVELS[i] || 0) * 9);
      var barHeight = Math.max(2, value * (height - 10));
      var alpha = 0.32 + 0.68 * (i / Math.max(1, count - 1));
      ctx.globalAlpha = alpha;
      ctx.fillStyle = accent;
      ctx.beginPath();
      var radius = barWidth / 2;
      var top = height / 2 - barHeight / 2;
      if (ctx.roundRect) {
        ctx.roundRect(x + i * (barWidth + gap), top, barWidth, barHeight, radius);
      } else {
        ctx.rect(x + i * (barWidth + gap), top, barWidth, barHeight);
      }
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  /* ───────────── 对话记录 ───────────── */

  function itemKey(item) {
    if (!item) { return ''; }
    return (item.ts || '') + '|' + (item.role || '') + '|' + (item.text || '');
  }

  function renderTranscript(items) {
    items = items || [];
    var box = $('transcript');
    var changed = items.length !== LAST_TRANSCRIPT.length;
    if (!changed) {
      for (var i = 0; i < items.length; i++) {
        if (itemKey(items[i]) !== itemKey(LAST_TRANSCRIPT[i])) { changed = true; break; }
      }
    }
    if (!changed) { return; }
    LAST_TRANSCRIPT = items;

    if (!items.length) {
      RENDERED = 0;
      box.innerHTML = '<div class="empty" id="transcript-empty">还没有对话。'
        + '喊一声唤醒词，或者在下面直接输入指令。</div>';
      return;
    }
    var empty = $('transcript-empty');
    if (empty) { empty.remove(); }

    // 追加式渲染：只补新增的条目，避免每 500ms 重播一次动画。
    // 只要「已经画出来的最后一条」对不上，就整体重画（历史被裁剪或清空过）。
    var start = RENDERED;
    var stillAligned = start <= items.length && LAST_TRANSCRIPT_OLD.length >= start;
    if (stillAligned && start > 0) {
      stillAligned = itemKey(items[start - 1]) === itemKey(LAST_TRANSCRIPT_OLD[start - 1]);
    }
    if (!stillAligned) {
      start = 0;
      box.innerHTML = '';
    }
    for (var index = start; index < items.length; index++) {
      box.appendChild(buildMessage(items[index]));
    }
    RENDERED = items.length;
    box.scrollTop = box.scrollHeight;
  }

  function buildMessage(item) {
    var wrap = document.createElement('div');
    wrap.className = 'msg ' + (item.role || 'system');
    var who = { user: '你', assistant: '助手', system: '系统' }[item.role] || item.role;
    wrap.innerHTML = '<div class="meta">' + esc(who) + ' · ' + esc(item.ts || '') + '</div>'
      + '<div class="bubble">' + esc(item.text) + '</div>';
    return wrap;
  }

  /* ───────────── 状态轮询 ───────────── */

  function stateName(status, data) {
    if (data.starting) { return 'think'; }
    if (!status.running) { return 'off'; }
    if (status.speaking) { return 'speak'; }
    if (status.state === 'listen') { return 'listen'; }
    if (status.state === 'think' || status.state === 'wait') { return 'think'; }
    return 'idle';
  }

  function renderState(data) {
    var status = data.status || {};
    document.body.dataset.state = stateName(status, data);
    $('state-text').textContent = data.starting ? '正在加载模型…' : (status.state_text || '未启动');
    $('pill-mode').textContent = status.mode || '离线规则';
    $('pill-mode').title = status.llm_ready ? 'LLM 大脑已启用' : '未配置 API Key，使用离线规则';

    var level = status.mic_level || 0;
    LEVELS.push(level);
    if (LEVELS.length > 48) { LEVELS.shift(); }
    drawWave(level);
    $('meter-value').textContent = level.toFixed(3);

    $('fact-hits').textContent = status.wake_hits || 0;
    $('fact-turns').textContent = status.turns || 0;
    $('fact-asr').textContent = (status.asr_rtf || 0).toFixed(3);
    $('fact-tts').textContent = (status.tts_rtf || 0).toFixed(3);
    $('fact-tools').textContent = (data.tools || 0) + ' / ' + ((data.skills || {}).ok || 0);

    renderChips(status.wake_words || []);

    var previous = LAST_TRANSCRIPT;
    LAST_TRANSCRIPT_OLD = previous;
    renderTranscript(status.transcript || []);

    var hint = $('boot-hint');
    if (data.last_error) {
      hint.className = 'hint error';
      hint.textContent = '启动失败：' + data.last_error;
    } else if ((data.missing_models || []).length) {
      hint.className = 'hint error';
      hint.textContent = '缺少模型：' + data.missing_models.join('、') + '；运行 python scripts/download_models.py';
    } else if (!status.running && !data.starting) {
      hint.className = 'hint';
      hint.textContent = '引擎未启动。点「启动监听」后喊唤醒词即可对话；也可以直接在中间输入文字指令。';
    } else {
      var follow = status.follow_up_ms || 0;
      hint.className = 'hint';
      hint.textContent = '正在监听。喊「' + ((status.wake_words || [])[0] || '唤醒词') + '」唤醒；'
        + (follow > 0 ? '回答后还有 ' + Math.round(follow / 1000) + ' 秒可以直接追问。' : '说「退下」结束。');
    }

    $('pill-engine-text').textContent = data.starting ? '启动中' : (status.running ? '监听中' : '未启动');
    $('btn-start').disabled = status.running || data.starting;
    $('btn-stop').disabled = !status.running;
  }

  var CHIPS = '';

  function renderChips(words) {
    var key = words.join('|');
    if (key === CHIPS) { return; }
    CHIPS = key;
    var box = $('wake-chips');
    box.innerHTML = '';
    words.forEach(function (word) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = word;
      box.appendChild(chip);
    });
  }

  function poll() {
    api('/api/state').then(renderState).catch(function () {
      $('pill-engine-text').textContent = '连接断开';
    });
  }

  /* ───────────── 日志 ───────────── */

  var TAG_CLASS = { asr: 't-asr', brain: 't-brain', agent: 't-agent', ui: 't-ui',
                    skills: 't-skills', tts: 't-tts', wake: 't-agent' };

  function appendLog(item) {
    if (!item || item.type !== 'log') { return; }
    var log = $('log');
    var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    var text = String(item.text || '');
    var match = text.match(/^\[([a-z_]+)\]/i);
    var cls = match ? (TAG_CLASS[match[1].toLowerCase()] || 't-default') : 't-default';

    var line = document.createElement('div');
    line.className = 'line';
    line.innerHTML = '<span class="time">' + esc(item.ts) + '</span>'
      + '<span class="tag ' + cls + '">' + esc(match ? '[' + match[1] + ']' : '') + '</span>'
      + '<span>' + esc(text.replace(/^\[[a-z_]+\]\s*/i, '')) + '</span>';
    log.appendChild(line);
    while (log.childElementCount > 500) { log.removeChild(log.firstChild); }
    if (atBottom) { log.scrollTop = log.scrollHeight; }
  }

  function connectEvents() {
    var source = new EventSource('/api/events?token=' + encodeURIComponent(TOKEN));
    source.onmessage = function (event) {
      try { appendLog(JSON.parse(event.data)); } catch (err) { /* 忽略坏帧 */ }
    };
    source.onerror = function () { /* EventSource 会自动重连 */ };
  }

  /* ───────────── 工具 ───────────── */

  var ALL_TOOLS = [];

  function renderTools() {
    var keyword = ($('tools-filter').value || '').trim().toLowerCase();
    var list = $('tools-list');
    list.innerHTML = '';
    var shown = ALL_TOOLS.filter(function (tool) {
      if (!keyword) { return true; }
      return (tool.name + ' ' + tool.title + ' ' + tool.description).toLowerCase().indexOf(keyword) >= 0;
    });
    if (!shown.length) { list.innerHTML = '<p class="note">没有匹配的工具。</p>'; return; }
    shown.forEach(function (tool) {
      var item = document.createElement('div');
      item.className = 'item';
      var badges = '';
      if (tool.confirm) { badges += '<span class="badge warn">需确认</span>'; }
      if (tool.source !== 'builtin') { badges += '<span class="badge ok">技能</span>'; }
      var params = tool.parameters.length
        ? '<span>参数：' + esc(tool.parameters.join('、')) + '</span>'
        : '<span>无参数</span>';
      item.innerHTML = '<div class="title"><span class="name">' + esc(tool.title) + '</span>'
        + '<span class="id">' + esc(tool.name) + '</span></div>'
        + '<p class="desc">' + esc(tool.description) + '</p>'
        + '<div class="meta">' + badges + params
        + '<button class="link" data-run="' + esc(tool.name) + '">试运行</button></div>';
      list.appendChild(item);
    });
    list.querySelectorAll('[data-run]').forEach(function (button) {
      button.addEventListener('click', function () { runTool(button.getAttribute('data-run')); });
    });
  }

  function loadTools() {
    return api('/api/tools').then(function (data) {
      ALL_TOOLS = data.items || [];
      renderTools();
      return ALL_TOOLS;
    });
  }

  function runTool(name) {
    var tool = null;
    ALL_TOOLS.forEach(function (item) { if (item.name === name) { tool = item; } });
    var args = {};
    if (tool && tool.parameters.length) {
      var raw = window.prompt('给「' + tool.title + '」传参数（JSON，可留空）：',
        '{\n  "' + tool.parameters[0] + '": ""\n}');
      if (raw === null) { return; }
      if (raw.trim()) {
        try { args = JSON.parse(raw); } catch (err) { toast('参数不是合法 JSON', 'err'); return; }
      }
    }
    api('/api/tools/call', { name: name, args: args }).then(function (res) {
      if (res.ok) { toast('返回：' + res.result, 'ok'); }
      else { toast(res.error || '执行失败', 'err'); }
    });
  }

  /* ───────────── 技能 ───────────── */

  function loadSkills() {
    return api('/api/skills').then(function (data) {
      $('skills-dirs').textContent = '项目目录：' + (data.project_dir || '')
        + '　用户目录：' + ((data.dirs || [])[1] || '');
      // 只有在用户没动过编辑框时才填入模板，否则会把人家写到一半的内容冲掉
      if (!SKILL_DIRTY && !SKILL_EDITING) {
        $('skill-body').value = data.template || '';
      }
      $('skill-hint').textContent = SKILL_EDITING
        ? ('正在编辑：' + SKILL_EDITING)
        : '保存后立即生效。返回的字符串会被朗读，写成人话、别返回 JSON。';

      var list = $('skills-list');
      list.innerHTML = '';
      var items = data.items || [];
      if (!items.length) {
        list.innerHTML = '<p class="note">还没有自定义技能。点「新建技能」写一个，'
          + '或者把 .yaml / .py 文件放进上面的目录。</p>';
        return;
      }
      items.forEach(function (skill) {
        var item = document.createElement('div');
        item.className = 'item' + (skill.error ? ' bad' : '');
        var badge = skill.error
          ? '<span class="badge err">加载失败</span>'
          : '<span class="badge ok">' + esc(skill.kind) + '</span>';
        item.innerHTML = '<div class="title"><span class="name">' + esc(skill.title || skill.name) + '</span>'
          + '<span class="id">' + esc((skill.tools || []).join(', ')) + '</span></div>'
          + '<p class="desc">' + esc(skill.error || skill.description || '（没有写描述）') + '</p>'
          + '<div class="meta">' + badge + '<span>' + esc(skill.source) + '</span>'
          + '<button class="link" data-edit="' + esc(skill.source) + '">编辑</button>'
          + '<button class="link" data-del="' + esc(skill.source) + '">删除</button></div>';
        list.appendChild(item);
      });
      list.querySelectorAll('[data-del]').forEach(function (button) {
        button.addEventListener('click', function () {
          var path = button.getAttribute('data-del');
          if (!window.confirm('确定删除 ' + path + ' 吗？')) { return; }
          api('/api/skills/delete', { path: path }).then(function (res) {
            toast(res.ok ? '已删除' : ('删除失败：' + res.error), res.ok ? 'ok' : 'err');
            loadSkills(); loadTools(); poll();
          });
        });
      });
      list.querySelectorAll('[data-edit]').forEach(function (button) {
        button.addEventListener('click', function () {
          var path = button.getAttribute('data-edit');
          api('/api/skills/read?token=' + encodeURIComponent(TOKEN) + '&path=' + encodeURIComponent(path))
            .then(function (res) {
              if (!res.ok) { toast(res.error || '读不到这个文件', 'err'); return; }
              SKILL_EDITING = res.path;
              SKILL_DIRTY = true;
              $('skill-name').value = res.name;
              $('skill-body').value = res.content;
              $('skill-modal-title').textContent = '编辑技能';
              $('skill-hint').textContent = '正在编辑：' + res.path;
              $('skill-modal').classList.remove('hidden');
            });
        });
      });
    });
  }

  function openNewSkill() {
    SKILL_EDITING = '';
    SKILL_DIRTY = false;
    $('skill-modal-title').textContent = '新建技能';
    $('skill-name').value = 'my_skill.yaml';
    api('/api/skills').then(function (data) {
      $('skill-body').value = data.template || '';
      $('skill-hint').textContent = '保存后立即生效。返回的字符串会被朗读，写成人话、别返回 JSON。';
    });
    $('skill-modal').classList.remove('hidden');
  }

  function closeSkillModal() {
    $('skill-modal').classList.add('hidden');
    SKILL_EDITING = '';
    SKILL_DIRTY = false;
  }

  /* ───────────── 配置 ───────────── */

  var FIELDS = [
    { key: 'wake.keywords', label: '唤醒词（逗号分隔）', type: 'text' },
    { key: 'wake.replies', label: '应答词（逗号分隔）', type: 'text' },
    { key: 'wake.threshold', label: '唤醒阈值（越低越灵敏，0.15~0.35）', type: 'number', step: 0.01 },
    { key: 'speech.profile', label: '资源档位', type: 'choice', options: ['fast', 'balanced', 'quality'] },
    { key: 'speech.device', label: '推理算力', type: 'choice', options: ['auto', 'cpu', 'cuda'] },
    { key: 'speech.threads', label: '线程数（0 = 按档位自动）', type: 'number' },
    { key: 'tts.enabled', label: '开启语音播报', type: 'bool' },
    { key: 'audio.output_gain', label: '输出音量（拖动实时生效）', type: 'volume' },
    { key: 'tts.engine', label: '合成引擎（vits 快；chattts 更自然但要显卡）', type: 'choice',
      options: ['vits', 'chattts'] },
    { key: 'tts.voice', label: '音色', type: 'voice' },
    { key: 'tts.speed', label: '语速', type: 'number', step: 0.05 },
    { key: 'llm.enabled', label: '启用 LLM 大脑', type: 'bool' },
    { key: 'llm.base_url', label: 'LLM 地址（OpenAI 兼容）', type: 'text' },
    { key: 'llm.model', label: '模型名', type: 'text' },
    { key: 'llm.reasoning_effort', label: '思考程度（越深越慢）', type: 'choice',
      options: ['off', 'low', 'medium', 'high', 'max'] },
    { key: 'llm.vision_max_side', label: '看图分辨率（截图最长边，像素）', type: 'number' },
    { key: 'llm.api_key', label: 'API Key（留空表示不改动）', type: 'password' },
    { key: 'agent.listen_timeout_ms', label: '唤醒后等待说话时长（毫秒）', type: 'number' },
    { key: 'agent.follow_up_ms', label: '追问窗口（毫秒，0 = 任务做完就回待命）', type: 'number' },
    { key: 'ui.show_turn', label: '主界面显示本轮问答', type: 'bool' },
    { key: 'ui.show_stats', label: '主界面底部显示速览四格', type: 'bool' },
    { key: 'agent.subagent_enabled', label: '开启子代理（多步任务丢到后台去做）', type: 'bool' },
    { key: 'agent.subagent_max', label: '同时最多几个子代理', type: 'number' },
    { key: 'agent.subagent_rounds', label: '每个子代理最多做几步', type: 'number' },
    { key: 'agent.subagent_announce', label: '子代理做完主动播报', type: 'bool' },
    { key: 'agent.cues', label: '开启收音 / 确认提示音', type: 'bool' },
    { key: 'agent.barge_in_wake', label: '播报时允许唤醒词打断', type: 'bool' },
    { key: 'agent.confirm.enabled', label: '敏感操作需要语音确认', type: 'bool' }
  ];

  var VOICES = null;

  function buildForm(settings) {
    var form = $('settings-form');
    form.innerHTML = '';
    FIELDS.forEach(function (field) {
      var value = settings[field.key];
      if (field.type === 'bool') {
        var row = document.createElement('label');
        row.className = 'check';
        row.innerHTML = '<input type="checkbox" data-key="' + field.key + '"'
          + (value ? ' checked' : '') + '><span>' + esc(field.label) + '</span>';
        form.appendChild(row);
        return;
      }
      var wrap = document.createElement('label');
      wrap.className = 'field';
      var shown = value == null ? '' : String(value);
      if (field.type === 'volume') {
        var pct = Math.round((Number(settings['audio.volume_percent']) || 0));
        wrap.innerHTML = '<span>' + esc(field.label) + '　<b id="volume-value">'
          + pct + '%</b></span>'
          + '<input type="range" min="0" max="150" step="5" data-kind="volume"'
          + ' value="' + pct + '">';
        form.appendChild(wrap);
        return;
      }
      if (field.type === 'voice') {
        var rows = (VOICES && VOICES.rows) || [];
        var picked = (VOICES && VOICES.current_name) || shown;
        var list = rows.map(function (row) {
          return '<option value="' + esc(row.name) + '"'
            + (row.name === picked ? ' selected' : '') + '>' + esc(row.label) + '</option>';
        }).join('');
        wrap.innerHTML = '<span>' + esc(field.label) + '</span>'
          + '<select data-key="' + esc(field.key) + '" data-kind="choice">' + list + '</select>'
          + '<button type="button" class="btn small" id="btn-audition">试听</button>';
        form.appendChild(wrap);
        return;
      }
      if (field.type === 'choice') {
        var options = (field.options || []).map(function (option) {
          return '<option value="' + esc(option) + '"'
            + (option === shown ? ' selected' : '') + '>' + esc(option) + '</option>';
        }).join('');
        wrap.innerHTML = '<span>' + esc(field.label) + '</span>'
          + '<select data-key="' + esc(field.key) + '" data-kind="choice">' + options + '</select>';
        form.appendChild(wrap);
        return;
      }
      var attrs = 'data-key="' + esc(field.key) + '" type="' + field.type + '"';
      if (field.step) { attrs += ' step="' + field.step + '"'; }
      wrap.innerHTML = '<span>' + esc(field.label) + '</span>'
        + '<input ' + attrs + ' value="' + esc(shown) + '">';
      form.appendChild(wrap);
    });
    // 这个按钮是跟着表单一起生成的，不属于 index.html 的静态结构，
    // 所以从 form 里找；用全局 $() 会被「id 必须在 index.html 里」的检查拦下（它拦得对）
    // 音量：拖动只改声音（不落盘），松手才存
    var volume = form.querySelector('[data-kind="volume"]');
    if (volume) {
      var volumeLabel = form.querySelector('#volume-value');
      var pushVolume = function (persist) {
        api('/api/volume', { percent: Number(volume.value), persist: persist })
          .then(function (res) {
            if (!res.ok) { toast(res.error || '音量没调成功', 'err'); }
          });
      };
      volume.addEventListener('input', function () {
        if (volumeLabel) { volumeLabel.textContent = volume.value + '%'; }
        pushVolume(false);
      });
      volume.addEventListener('change', function () { pushVolume(true); });
    }

    var audition = form.querySelector('#btn-audition');
    if (audition) {
      audition.addEventListener('click', function () {
        var select = form.querySelector('[data-key="tts.voice"]');
        if (!select || !select.value) { return; }
        api('/api/voices/audition', { voice: select.value }).then(function (res) {
          toast(res.ok ? ('正在试听 ' + (res.voice || '')) : (res.error || '试听失败'),
            res.ok ? 'ok' : 'err');
        });
      });
    }

    var info = document.createElement('p');
    info.className = 'note';
    info.textContent = '算力：' + (settings['speech.provider_text'] || '')
      + '　合成：' + (settings['tts.engine'] || '')
      + '　' + (settings['speech.profile_note'] || '')
      + '\n模型目录：' + (settings['models_dir'] || '')
      + '　配置文件：' + (settings['config_path'] || '');
    form.appendChild(info);
    if (VOICES && VOICES.note) {
      var warn = document.createElement('p');
      warn.className = 'note';
      warn.textContent = VOICES.note;
      form.appendChild(warn);
    }
  }

  function loadSettings() {
    // 音色清单要一起拿：下拉框里给的是名字（zf_003），不是让人猜的编号
    return Promise.all([
      api('/api/settings'),
      api('/api/voices').catch(function () { return null; })
    ]).then(function (pair) {
      var data = pair[0];
      VOICES = (pair[1] && pair[1].rows) ? pair[1] : null;
      if (!data.ok) { toast(data.error || '读不到设置', 'err'); return; }
      buildForm(data.settings || {});
    });
  }

  function collectSettings() {
    var updates = {};
    $('settings-form').querySelectorAll('[data-key]').forEach(function (input) {
      var key = input.getAttribute('data-key');
      if (input.type === 'checkbox') { updates[key] = input.checked; return; }
      // 音量走 /api/volume 专用接口（要实时生效），别当普通设置存进来
      if (input.getAttribute('data-kind') === 'volume') { return; }
      if (input.getAttribute('data-kind') === 'choice') {
        var picked = input.value.trim();
        if (picked) { updates[key] = picked; }
        return;
      }
      var raw = input.value.trim();
      // 只有 API Key 是「留空 = 不改动」；其它字段清空就是真的想清空，
      // 之前一刀切跳过空值，用户清了地址却被告知保存成功，配置其实没变
      if (key === 'llm.api_key' && raw === '') { return; }
      if (input.type === 'number') {
        if (raw === '') { return; }
        updates[key] = Number(raw);
      } else if (key === 'wake.keywords' || key === 'wake.replies') {
        updates[key] = raw.split(/[,，、]/).map(function (part) { return part.trim(); })
          .filter(function (part) { return part.length; });
      } else {
        updates[key] = raw;
      }
    });
    return updates;
  }

  function loadRawConfig() {
    return api('/api/config').then(function (data) {
      if (!data.ok) { toast(data.error || '读不到配置', 'err'); return; }
      $('raw-config').value = data.text || '';
      $('config-path').textContent = data.path || '';
    });
  }

  /* ───────────── 设备 ───────────── */

  function fillSelect(select, items, current) {
    select.innerHTML = '';
    var auto = document.createElement('option');
    auto.value = '';
    auto.textContent = '系统默认';
    select.appendChild(auto);
    (items || []).forEach(function (device) {
      var option = document.createElement('option');
      option.value = String(device.index);
      option.textContent = '[' + device.index + '] ' + device.name + (device.default ? '（默认）' : '');
      select.appendChild(option);
    });
    select.value = current == null ? '' : String(current);
  }

  function loadDevices() {
    return api('/api/devices').then(function (data) {
      if (!data.ok) { toast(data.error || '读不到音频设备', 'err'); return; }
      var current = data.current || {};
      fillSelect($('dev-in'), data.input, current.input);
      fillSelect($('dev-out'), data.output, current.output);
    });
  }

  /* ───────────── 事件绑定 ───────────── */

  function bind() {
    $('btn-theme').addEventListener('click', function () {
      applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    });

    $('btn-start').addEventListener('click', function () {
      toast('正在加载模型，请稍候…');
      api('/api/engine', { action: 'start' }).then(function (res) {
        if (!res.ok) { toast(res.error || '启动失败', 'err'); }
      });
    });
    $('btn-stop').addEventListener('click', function () {
      api('/api/engine', { action: 'stop' }).then(function () { toast('已停止监听', 'ok'); });
    });
    $('btn-cancel').addEventListener('click', function () {
      api('/api/engine', { action: 'cancel' }).then(function () { toast('已打断', 'ok'); });
    });

    function sendCommand() {
      var input = $('cmd-input');
      var text = input.value.trim();
      if (!text) { return; }
      input.value = '';
      api('/api/command', { text: text }).then(function (res) {
        if (!res.ok) { toast(res.error || '执行失败', 'err'); return; }
        if (!res.spoke) {
          toast('已执行（引擎未启动，只返回文字）', 'ok');
          api('/api/state').then(renderState);
        }
      });
    }

    $('btn-send').addEventListener('click', sendCommand);
    $('cmd-input').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { sendCommand(); }
    });
    $('btn-say').addEventListener('click', function () {
      var text = $('cmd-input').value.trim();
      if (!text) { toast('先输入要播报的内容'); return; }
      $('cmd-input').value = '';
      api('/api/say', { text: text }).then(function (res) {
        toast(res.ok ? '正在播报' : (res.error || '播报失败'), res.ok ? 'ok' : 'err');
      });
    });

    $('btn-clear-log').addEventListener('click', function () { $('log').innerHTML = ''; });
    $('btn-toggle-log').addEventListener('click', function () {
      var log = $('log');
      log.classList.toggle('collapsed');
      this.textContent = log.classList.contains('collapsed') ? '运行日志 ▸' : '运行日志 ▾';
    });
    $('btn-clear-chat').addEventListener('click', function () {
      $('transcript').innerHTML = '<div class="empty" id="transcript-empty">'
        + '还没有对话。喊一声唤醒词，或者在下面直接输入指令。</div>';
      LAST_TRANSCRIPT = []; LAST_TRANSCRIPT_OLD = []; RENDERED = 0;
    });
    $('btn-listen').addEventListener('click', function () {
      toast('请说话，最长 12 秒…');
      api('/api/listen', { timeout: 12 }).then(function (res) {
        toast(res.ok ? ('识别到：' + res.text) : (res.error || '录音失败'), res.ok ? 'ok' : 'err');
      });
    });

    document.querySelectorAll('.tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        document.querySelectorAll('.tab').forEach(function (other) { other.classList.remove('active'); });
        tab.classList.add('active');
        ['tools', 'skills', 'config', 'devices'].forEach(function (name) {
          $('tab-' + name).classList.toggle('hidden', name !== tab.dataset.tab);
        });
        if (tab.dataset.tab === 'config') { loadSettings(); loadRawConfig(); }
        if (tab.dataset.tab === 'devices') { loadDevices(); }
        if (tab.dataset.tab === 'skills') { loadSkills(); }
      });
    });

    $('tools-filter').addEventListener('input', renderTools);
    $('btn-reload-skills').addEventListener('click', function () {
      api('/api/skills/reload', {}).then(function (res) {
        toast(res.ok ? '技能已重新加载' : '重新加载失败', res.ok ? 'ok' : 'err');
        loadSkills(); loadTools(); poll();
      });
    });
    $('btn-new-skill').addEventListener('click', openNewSkill);
    $('btn-close-skill').addEventListener('click', closeSkillModal);
    $('skill-body').addEventListener('input', function () { SKILL_DIRTY = true; });
    $('btn-save-skill').addEventListener('click', function () {
      api('/api/skills/save', {
        filename: $('skill-name').value.trim(),
        content: $('skill-body').value
      }).then(function (res) {
        if (res.ok) {
          toast('技能已保存并加载', 'ok');
          closeSkillModal();
          loadSkills(); loadTools(); poll();
        } else {
          toast('保存失败：' + res.error, 'err');
        }
      });
    });
    $('btn-run-skill').addEventListener('click', function () {
      var name = ($('skill-name').value.trim() || '').replace(/\.(yaml|yml|py)$/i, '');
      runTool(name);
    });

    $('btn-save-settings').addEventListener('click', function () {
      api('/api/config', { updates: collectSettings() }).then(function (res) {
        if (res.ok) {
          toast(res.message + (res.restart_needed ? '（重启引擎后生效）' : ''), 'ok');
          loadRawConfig(); poll();
        } else {
          toast(res.error || '保存失败', 'err');
        }
      });
    });
    $('btn-toggle-raw').addEventListener('click', function () {
      $('raw-wrap').classList.toggle('hidden');
    });
    $('btn-save-raw').addEventListener('click', function () {
      api('/api/config', { text: $('raw-config').value }).then(function (res) {
        toast(res.ok ? (res.message || '已保存') : (res.error || '保存失败'), res.ok ? 'ok' : 'err');
        if (res.ok) { loadSettings(); poll(); }
      });
    });

    $('btn-save-devices').addEventListener('click', function () {
      var updates = {
        'audio.input_device': $('dev-in').value === '' ? null : Number($('dev-in').value),
        'audio.output_device': $('dev-out').value === '' ? null : Number($('dev-out').value)
      };
      api('/api/config', { updates: updates }).then(function (res) {
        if (!res.ok) { toast(res.error || '保存失败', 'err'); return; }
        toast('设备已保存，正在重启引擎…', 'ok');
        api('/api/engine', { action: 'stop' }).then(function () {
          api('/api/engine', { action: 'start' });
        });
      });
    });
    $('btn-refresh-devices').addEventListener('click', loadDevices);

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        if (!$('skill-modal').classList.contains('hidden')) { closeSkillModal(); return; }
        api('/api/engine', { action: 'cancel' });
      }
    });
  }

  /* ───────────── 启动 ───────────── */

  initTheme();
  bind();
  connectEvents();
  poll();
  setInterval(poll, 500);
  loadTools();
  loadSkills();
  loadSettings();
  loadRawConfig();
  drawWave(0);
})();
