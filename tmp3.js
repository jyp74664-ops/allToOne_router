>
    /* ============ 鉴权与 fetch 封装 ============ */
    const TOKEN_KEY = 'gateway_admin_token';
    function getToken() { return "{{ local_api_key }}"; }
    function setToken(t) { }
    function clearToken() { }

    function showGate() { }
    async function submitGate() { }

    async function apiFetch(url, options = {}) {
      const token = getToken();
      if (token) {
        options.headers = Object.assign({}, options.headers || {}, { 'Authorization': 'Bearer ' + token });
      }
      const r = await fetch(url, options);
      if (r.status === 401) {
        clearToken();
        showGate();
        throw new Error('Unauthorized');
      }
      return r;
    }

    /* ============ Toast 提示 ============ */
    let _toastTimer = null;
    function showToast(msg, type = 'ok') {
      const el = document.getElementById('toast');
      if (!el) return;
      el.textContent = msg;
      el.className = 'toast show ' + type;
      if (_toastTimer) clearTimeout(_toastTimer);
      _toastTimer = setTimeout(() => { el.className = 'toast ' + type; }, 3000);
    }

    /* ============ 数据 ============ */
    let data = [];
    let stabilityData = [];
    let modelDetails = {};
    let circuitBreakerData = {};  // circuit breaker 状态: { "provider||model": {fails, open_until} }

    function copyText(text, btn) {
      navigator.clipboard.writeText(text).then(() => {
        const oldText = btn.innerText;
        btn.innerText = '已复制';
        btn.style.background = 'rgba(50, 215, 75, 0.2)';
        btn.style.color = '#32D74B';
        setTimeout(() => { btn.innerText = oldText; btn.style.background = ''; btn.style.color = ''; }, 2000);
      });
    }

    async function load() {
      try {
        const r = await apiFetch('/api/providers');
        const list = await r.json();
        data = [];
        window.modelEnabledMap = {};

        let providersHtml = '';
        let providerOptionsHtml = '<option value="all">全部提供商</option>';
        list.forEach(p => {
          providerOptionsHtml += `<option value="${p.name}">${p.name}</option>`;
          providersHtml += `
        <div class="provider-card">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; gap: 6px; flex-wrap: wrap;">
            <div class="p-name ${(p.provider_status === 'error' || !p.models || p.models.length === 0) ? 'provider-error' : ''}" style="margin-bottom: 0;" title="${(p.provider_status === 'error' ? '上游 API 不可用' : '') + (!p.models || p.models.length === 0 ? (p.provider_status === 'error' ? '；' : '上游模型列表为空') : '')}">${p.name} Configuration</div>
            <div style="display: flex; gap: 6px; flex-shrink: 0;">
              <button class="btn" style="padding: 4px 8px; font-size: 11px;" onclick="openModelManager('${p.name}')">⚙️ 模型</button>
              <button class="btn" style="padding: 4px 8px; font-size: 11px;" onclick="openProviderEditor('${p.name}')">✏️ 编辑</button>
              <button class="btn" style="padding: 4px 8px; font-size: 11px; color: var(--danger);" onclick="deleteProvider('${p.name}')">🗑 删除</button>
            </div>
          </div>
          <div class="copy-row">
            <span>Base URL:</span>
            <code>${p.base_url}</code>
            <button class="btn-copy" onclick="copyText('${p.base_url}', this)">Copy</button>
          </div>
          <div class="copy-row">
            <span>API Key:</span>
            <code>${p.api_key_masked || '****'}</code>
          </div>
        </div>
      `;
          (p.models || []).forEach(m => {
            window.modelEnabledMap[`${p.name}||${m}`] = !(p.disabled_models || []).includes(m);
            const h = p.health?.[m] || { status: 'unknown' };
            const d = modelDetails[m] || {};
            const ctxLen = d.context_length || d.max_model_len || '--';
            data.push({
              provider: p.name, model: m, status: h.status || 'unknown',
              latency_ms: h.latency_ms ?? null, code: h.code ?? null,
              context_length: ctxLen
            });
          });
          // 限额型模型也加入监控列表，展示健康状态
          (p.disabled_models || []).forEach(m => {
            window.modelEnabledMap[`${p.name}||${m}`] = false;
            const h = p.health?.[m] || { status: 'unknown' };
            const d = modelDetails[m] || {};
            const ctxLen = d.context_length || d.max_model_len || '--';
            data.push({
              provider: p.name, model: m, status: h.status || 'unknown',
              latency_ms: h.latency_ms ?? null, code: h.code ?? null,
              context_length: ctxLen, _disabled: true
            });
          });
        });

        // 同步到上游提供商页签
        document.getElementById('providersSection-provider').innerHTML = providersHtml;

        const providerSelect = document.getElementById('providerFilter');
        const currentVal = providerSelect.value;
        providerSelect.innerHTML = providerOptionsHtml;
        if ([...providerSelect.options].some(o => o.value === currentVal)) {
          providerSelect.value = currentVal;
        }
        // 没有历史巡检记录时，也展示当前已配置模型。
        if (!stabilityData.length && data.length) {
          stabilityData = data.map(item => ({
            provider: item.provider,
            model: item.model,
            availability: 0,
            checks: 0,
            ok: 0,
            avg_latency_ms: null,
            min_latency_ms: null,
            max_latency_ms: null,
            last_status: item.status,
          }));
        }
        renderStability();
      } catch (e) {
        if (e.message !== 'Unauthorized') console.error("Failed to load data:", e);
      }
    }

    async function syncAllFreeModels() {
      const btn = document.getElementById('syncFreeModelsBtn');
      const originalText = btn.innerText;
      btn.disabled = true;
      btn.innerText = '同步中...';
      try {
        const r = await apiFetch('/api/providers/sync-free-models', { method: 'POST' });
        const data = await r.json();
        const success = (data.results || []).filter(item => item.ok);
        const failed = (data.results || []).filter(item => !item.ok);
        const total = success.reduce((sum, item) => sum + item.count, 0);
        showToast(`同步完成：${success.length} 个提供商，${total} 个免费模型${failed.length ? `，${failed.length} 个失败` : ''}`, failed.length ? 'warn' : 'ok');
        await load();
      } catch (e) {
        if (e.message !== 'Unauthorized') {
          console.error('Sync free models failed', e);
          showToast('同步免费模型失败', 'error');
        }
      } finally {
        btn.disabled = false;
        btn.innerText = originalText;
      }
    }

    async function loadStability(hours, renderFn) {
      const h = hours || document.getElementById('stabilityHours').value;
      try {
        const r = await apiFetch(`/api/stability?hours=${h}`);
        stabilityData = await r.json();
        if (!stabilityData.length && data.length) {
          stabilityData = data.map(item => ({
            provider: item.provider,
            model: item.model,
            availability: 0,
            checks: 0,
            ok: 0,
            avg_latency_ms: null,
            min_latency_ms: null,
            max_latency_ms: null,
            last_status: item.status,
          }));
        }
        (renderFn || renderStability)();
      } catch (e) {
        if (e.message !== 'Unauthorized') console.error("Failed to load stability:", e);
      }
    }

    let usageDays = 1;
    async function loadUsage() {
      try {
        const r = await apiFetch(`/api/usage?days=${usageDays}`);
        const data = await r.json();
        renderUsage(data);
      } catch (e) {
        if (e.message !== 'Unauthorized') console.error("Failed to load usage:", e);
      }
    }
    function switchUsageTab(days) {
      usageDays = days;
      document.querySelectorAll('.usage-tab').forEach(b => {
        b.classList.toggle('btn-primary', Number(b.dataset.days) === days);
      });
      loadUsage();
    }
    function fmtNum(n) {
      n = n || 0;
      if (n < 10000) return n.toLocaleString();
      if (n < 100000000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + ' 万';
      return (n / 100000000).toFixed(2).replace(/\.?0+$/, '') + ' 亿';
    }
    function renderUsage(data) {
      const t = data.total || {};
      const cards = [
        { label: '输入 Token', value: t.pt },
        { label: '输出 Token', value: t.ct },
        { label: '合计 Token', value: t.tt },
        { label: '请求数', value: t.requests },
      ];
      document.getElementById('usageOverview').innerHTML = cards.map(c =>
        `<div class="provider-card" style="padding: 14px 16px;">
       <div style="font-size: 12px; color: var(--text-secondary);">${c.label}</div>
       <div style="font-size: 22px; font-weight: 600; color: var(--text-primary); margin-top: 4px;">${fmtNum(c.value)}</div>
     </div>`
      ).join('');
      const rows = data.by_model || [];
      document.getElementById('usageBody').innerHTML = rows.length
        ? rows.map(r => `<tr>
        <td>
          <div class="model-name" style="display: flex; align-items: center;">
            <span class="model-provider-badge">${r.provider || 'unknown'}</span>
            <span>${r.model || 'unknown'}</span>
          </div>
        </td>
        <td>${fmtNum(r.requests)}</td>
        <td>${fmtNum(r.pt)}</td>
        <td>${fmtNum(r.ct)}</td>
        <td>${fmtNum(r.tt)}</td>
      </tr>`).join('')
        : '<tr><td colspan="5" class="empty">暂无消耗记录</td></tr>';
    }

    let sortCol = null;
    let sortAsc = true;

    async function syncRateLimits() {
      const btn = document.getElementById('syncRateBtn');
      // 收集当前所有模型的速率限制
      const limits = {};
      stabilityData.forEach(d => {
        const detail = modelDetails[d.model] || {};
        if (detail.rate_limit) limits[d.model] = detail.rate_limit;
      });
      const template = JSON.stringify({ rate_limits: limits }, null, 2);

      const content = await showEditModal('⏱ 速率限制（手动编辑）',
        '格式：{"模型名": "15 RPM(15请求/分钟)"}。支持 RPM/RPD/TPM/TPD/RPS。\n也可输入 {"sync": true} 从 GitHub 自动同步。\n⚠️ 注意：GitHub 访问可能较慢或超时。',
        template);

      if (content === null) return;

      // 按钮 loading 状态
      const originalText = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '⏳ 处理中...';

      try {
        const body = JSON.parse(content);
        const r = await apiFetch('/api/sync-rate-limits', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }, 45000); // 45秒超时
        const d = await r.json();
        if (d.ok) {
          const extra = d.total_providers ? `（来源 ${d.total_providers} 个公开 API 源，匹配 ${d.matched} 个模型）` : '';
          const msg = body.sync
            ? `✅ 自动同步完成！${extra}`
            : `✅ 速率限制已保存！${extra}`;
          showToast(msg, 'ok');
          await loadModelDetails();
          renderStability();
        } else {
          const errMsg = d.detail || '未知错误';
          showToast('❌ 同步失败：' + errMsg, 'error');
        }
      } catch (e) {
        let errMsg = '网络请求失败';
        if (e.message && e.message.includes('AbortError')) errMsg = '请求已取消';
        else if (e.message) errMsg = e.message;
        showToast('❌ ' + errMsg, 'error');
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
      }
    }

    function showEditModal(title, hint, initialValue) {
      return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:10000;display:flex;align-items:center;justify-content:center;';
        overlay.innerHTML = `<div style="background:var(--bg-card);border-radius:16px;padding:24px;width:700px;max-width:95vw;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,0.3);">
      <h3 style="margin:0 0 8px 0;color:var(--text-primary);">${title}</h3>
      <p style="margin:0 0 12px 0;color:var(--text-secondary);font-size:13px;white-space:pre-line;">${hint}</p>
      <textarea id="editModalTextarea" style="flex:1;min-height:300px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:ui-monospace,monospace;font-size:13px;resize:vertical;tab-size:2;">${initialValue}</textarea>
      <div style="display:flex;gap:12px;justify-content:flex-end;margin-top:16px;">
        <button class="btn" onclick="this.closest('.modal-overlay').remove();window._editModalResolve(null);">取消</button>
        <button class="btn btn-primary" onclick="const v=document.getElementById('editModalTextarea').value;this.closest('.modal-overlay').remove();window._editModalResolve(v);">保存</button>
      </div>
    </div>`;
        document.body.appendChild(overlay);
        window._editModalResolve = resolve;
        overlay.querySelector('textarea').focus();
      });
    }

    function renderStability() {
      const filter = document.getElementById('stableFilter').value;
      const search = document.getElementById('stableSearch').value.toLowerCase();
      const provider = document.getElementById('providerFilter').value;

      let filtered = stabilityData.filter(d => {
        if (provider !== 'all' && d.provider !== provider) return false;
        if (search && !d.model.toLowerCase().includes(search)) return false;
        // Add the new condition for the switch
        if (document.getElementById('hideZeroSuccess').checked && d.ok === 0) return false;
        if (filter === 'high') return d.availability >= 90;
        if (filter === 'mid') return d.availability >= 50 && d.availability < 90;
        if (filter === 'low') return d.availability < 50;
        return true;
      });

      /* ---- 排序 ---- */
      if (sortCol) {
        const asc = sortAsc;
        filtered.sort((a, b) => {
          let va, vb;
          switch (sortCol) {
            case 'model':
              va = a.provider + ' ' + a.model; vb = b.provider + ' ' + b.model;
              return asc ? va.localeCompare(vb, 'zh') : vb.localeCompare(va, 'zh');
            case 'size':
              // Compare parameter count (B) — strip ~ prefix before parsing
              const getSize = (m) => {
                const s = (modelDetails[m] || {}).size;
                if (s) return parseFloat(s.replace(/^~/, '')) || 0;
                const match = m.match(/(\d+(?:\.\d+)?)b/i);
                return match ? parseFloat(match[1]) : 0;
              };
              const sizeA = getSize(a.model);
              const sizeB = getSize(b.model);
              return asc ? sizeA - sizeB : sizeB - sizeA;
            case 'availability':
              va = parseFloat(a.availability) || 0; vb = parseFloat(b.availability) || 0;
              return asc ? va - vb : vb - va;
            case 'context_length': {
              const da = modelDetails[a.model] || {}; const db = modelDetails[b.model] || {};
              va = parseInt(da.context_length || da.max_model_len) || 0;
              vb = parseInt(db.context_length || db.max_model_len) || 0;
              return asc ? va - vb : vb - va;
            }
            case 'rate_limit':
              va = (modelDetails[a.model] || {}).rate_limit || ''; vb = (modelDetails[b.model] || {}).rate_limit || '';
              return asc ? va.localeCompare(vb, 'zh') : vb.localeCompare(va, 'zh');
            case 'checks':
              va = a.checks || 0; vb = b.checks || 0;
              return asc ? va - vb : vb - va;
            case 'ok':
              va = a.ok || 0; vb = b.ok || 0;
              return asc ? va - vb : vb - va;
            case 'avg_latency_ms':
              va = a.avg_latency_ms ?? Infinity; vb = b.avg_latency_ms ?? Infinity;
              return asc ? va - vb : vb - va;
            case 'min_latency_ms':
              va = a.min_latency_ms ?? Infinity; vb = b.min_latency_ms ?? Infinity;
              return asc ? va - vb : vb - va;
            default:
              return 0;
          }
        });
      }

      /* 更新表头排序指示 */
      document.querySelectorAll('th.sortable').forEach(th => {
        th.classList.toggle('active', th.dataset.sort === sortCol);
        const icon = th.querySelector('.sort-icon');
        if (icon) icon.textContent = (th.dataset.sort === sortCol)
          ? (sortAsc ? '↑' : '↓') : '⇅';
      });

      const tbody = document.getElementById('stableBody');
      if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">No records found in this timeframe.</td></tr>';
        return;
      }

      tbody.innerHTML = filtered.map(d => {
        const pct = typeof d.availability === 'number' ? d.availability.toFixed(1) : parseFloat(d.availability || 0).toFixed(1);
        const barColor = pct >= 90 ? 'var(--success)' : pct >= 50 ? 'var(--warning)' : 'var(--danger)';
        const formatLatency = value => Number.isFinite(Number(value)) ? Math.round(Number(value)) + ' ms' : '--';
        const avgL = formatLatency(d.avg_latency_ms);
        const minL = formatLatency(d.min_latency_ms);
        const maxL = formatLatency(d.max_latency_ms);
        const checks = Number(d.checks) || 0;
        const ok = Number(d.ok) || 0;
        const fail = (Number(d.fail) || 0) + (Number(d.error) || 0);

        // 判断是否为无数据/空状态
        const isEmpty = checks === 0;
        const badgeClass = isEmpty ? 'model-provider-badge badge-error' : 'model-provider-badge';
        const isDisabled = d._disabled;

        const detail = modelDetails[d.model] || {};
        const ctxLen = detail.context_length || detail.max_model_len || '--';
        const descHtml = detail.desc ? `<div class="model-desc">${detail.desc}</div>` : '';
        const rateLimit = detail.rate_limit || '--';
        const rateLimitRaw = typeof rateLimit === 'object' ? (rateLimit.raw || '--') : rateLimit;
        const rateLimitTip = typeof rateLimit === 'object' ? (rateLimit.tooltip || '') : '';

        let lastStatusHtml = '';
        if (d.last_status === 'ok') {
          lastStatusHtml = `<span title="上次巡检：正常" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--success);margin-left:6px;"></span>`;
        } else if (d.last_status === 'fail' || d.last_status === 'error') {
          lastStatusHtml = `<span title="上次巡检：异常 (通常为提供商限流或报错)" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--danger);margin-left:6px;animation: pulse 2s infinite;"></span>`;
        } else {
          lastStatusHtml = `<span title="上次巡检：等待中..." style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--text-secondary);margin-left:6px;"></span>`;
        }

        const disabledBadge = isDisabled ? ' <span style="font-size:10px;background:var(--warning);color:#000;padding:1px 5px;border-radius:3px;margin-left:4px;">限额型</span>' : '';
        const rowStyle = isDisabled ? 'opacity:0.7;' : '';

        return `<tr style="${rowStyle}">
      <td>
        <div class="model-name" style="display: flex; align-items: center;">
          <span class="copy-model-icon" data-model="${d.provider}-${d.model}" title="复制模型名" style="cursor:pointer;margin-right:6px;opacity:0.35;font-size:14px;transition:opacity 0.2s;flex-shrink:0;" onmouseenter="this.style.opacity='1'" onmouseleave="this.style.opacity='0.35'">📋</span>
          <span class="${badgeClass}">${d.provider}</span>
          <span style="${isDisabled ? 'text-decoration:line-through;color:var(--text-secondary);' : ''}">${d.model}</span>${disabledBadge}
          ${lastStatusHtml}
        </div>
        ${descHtml}
      </td>
        ${descHtml}
      </td>
      <td class="size-cell">${formatSize((detail.size || (d.model.match(/(\d+(?:\.\d+)?)b/i) ? d.model.match(/(\d+(?:\.\d+)?)b/i)[1] + 'B' : '--')), d.model)}</td>
      <td>
        <div class="avail-wrap">
          <span class="avail-bar">
            <span class="avail-bar-fill" style="width:${pct}%;background:${barColor}"></span>
          </span>
          <span class="avail-text" style="color:${barColor}">${pct}%</span>
        </div>
      </td>
      <td><span class="ctx-cell" data-model="${d.model}" title="点击修改上下文长度" style="cursor:pointer;color:var(--text-secondary);font-variant-numeric:tabular-nums;font-family:ui-monospace,monospace;font-size:12px;">${ctxLen === '--' ? '--' : ctxLen}</span></td>
      <td style="font-size:11px;color:var(--text-secondary);max-width:200px;white-space:normal;word-break:break-word;"${rateLimitTip ? ` title="${rateLimitTip}"` : ''}>${rateLimitRaw}</td>
      <td style="color:var(--text-secondary)">${checks}</td>
      <td style="font-variant-numeric:tabular-nums; font-size:12px;">
        <span style="color:var(--success);font-weight:600">${ok}</span>
        <span style="color:var(--text-secondary);margin:0 4px">/</span>
        <span style="color:var(--danger);font-weight:600">${fail}</span>
      </td>
      <td class="latency ${d.avg_latency_ms && d.avg_latency_ms < 3000 ? 'fast' : d.avg_latency_ms && d.avg_latency_ms < 8000 ? 'medium' : ''}">${avgL}</td>
      <td class="latency" style="font-size:12px;">
        <span style="color:var(--success)">${minL}</span>
        <span style="color:var(--text-secondary);margin:0 4px">~</span>
        <span style="color:var(--text-secondary)">${maxL}</span>
      </td>
    </tr>`;
      }).join('');
    }

    /* ---- 表头排序点击 ---- */
    document.addEventListener('click', e => {
      const th = e.target.closest('th.sortable');
      if (!th) return;
      const col = th.dataset.sort;
      if (sortCol === col) {
        sortAsc = !sortAsc;
      } else {
        sortCol = col;
        sortAsc = true;
      }
      renderStability();
    });

    /* ---- 模型名复制图标点击 ---- */
    document.addEventListener('click', e => {
      const icon = e.target.closest('.copy-model-icon');
      if (!icon) return;
      e.stopPropagation();
      const modelName = icon.dataset.model;
      if (!modelName) return;
      // 优先 clipboard API，失败则 fallback 到 textarea
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(modelName).then(() => {
          icon.textContent = '✅'; icon.style.opacity = '1';
          setTimeout(() => { icon.textContent = '📋'; icon.style.opacity = '0.35'; }, 1500);
        }).catch(() => fallbackCopy(modelName, icon));
      } else {
        fallbackCopy(modelName, icon);
      }
    });
    function fallbackCopy(text, icon) {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (_) { }
      document.body.removeChild(ta);
      if (icon) { icon.textContent = '📋'; icon.style.opacity = '0.35'; }
    }

    async function checkAll() {
      const btn = document.getElementById('checkAllBtn');
      const originalHtml = btn.innerHTML;
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="animation: spin 1s linear infinite"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg> 检测中...';
      btn.disabled = true;
      const statusEl = document.getElementById('pollStatus');
      const originalStatus = statusEl.innerHTML;
      
      let ok = 0, fail = 0, total = 0, current = 0;
      let abortController = null;
      
      try {
        abortController = new AbortController();
        const token = getToken();
        
        const response = await fetch('/api/check/all', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + token },
          signal: abortController.signal
        });
        
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const data = line.slice(6);
            
            try {
              const event = JSON.parse(data);
              
              if (event.done) {
                const results = event.results || {};
                ok = Object.values(results).filter(v => v && v.status === 'ok').length;
                fail = Object.values(results).filter(v => !['ok', 'cooldown'].includes(v.status)).length;
                showToast(`检测完成：${ok} 正常 / ${fail} 异常`, fail > 0 ? 'warn' : 'ok');
              } else if (event.provider && event.model) {
                total = event.total;
                current = event.current;
                if (event.status === 'cooldown') {
                  statusEl.innerHTML = `⏳ 跳过限速模型 ${current}/${total} | ${event.provider} · ${event.model}`;
                } else {
                  const icon = event.status === 'ok' ? '✓' : event.status === 'error' ? '✗' : '?';
                  const color = event.status === 'ok' ? 'var(--success)' : event.status === 'error' ? 'var(--danger)' : 'var(--unknown)';
                  statusEl.innerHTML = `🔄 检测中 ${current}/${total} | ${event.provider} · ${event.model} <span style="color:${color};font-weight:600">${icon}</span>`;
                }
              }
            } catch (e) {
              // skip malformed events
            }
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError' && e.message !== 'Unauthorized') {
          console.error("Check failed", e);
          showToast("检测请求失败", "error");
        }
      } finally {
        btn.innerHTML = originalHtml;
        btn.disabled = false;
        statusEl.innerHTML = originalStatus;
        load();
      }
    }

    async function updatePollStatus() {
      try {
        const r = await apiFetch('/api/poll-status');
        const d = await r.json();
        if (d.last_check_time > 0) {
          const date = new Date(d.last_check_time * 1000);
          const pad = n => String(n).padStart(2, '0');
          const timeStr = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
          document.getElementById('pollStatus').innerHTML = `上次检测：<span style="color:var(--text-primary);font-weight:500">${timeStr}</span> <span style="margin:0 8px;color:rgba(255,255,255,0.2)">|</span> 已扫描 <span style="color:var(--text-primary);font-weight:500">${d.total_models}</span> 个模型`;
        } else {
          document.getElementById('pollStatus').textContent = `尚未检测… ${d.total_models} 个模型待扫描`;
        }
      } catch (e) {
        if (e.message !== 'Unauthorized') { }
      }
    }

    let currentManagingProvider = null;
    let modalSortCol = null;
    let modalSortAsc = true;
    let modalStabilityMapForModal = {};

    /* 从模型名中提取大小（如 7b, 14b, 70b 等） */
    function parseModelSize(modelName) {
      const match = modelName.match(/(\d+(?:\.\d+)?)\s*b/i);
      return match ? parseFloat(match[1]) : null;
    }

    async function openModelManager(provider) {
      currentManagingProvider = provider;
      modalSortCol = null;
      modalSortAsc = true;
      document.getElementById('modalTitle').innerText = `${provider} - 模型管理`;
      document.getElementById('modalSearchInput').value = '';
      document.getElementById('modalModelList').innerHTML = '<div class="empty">正在获取远端可用模型列表...<br><br><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="animation: spin 1s linear infinite"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="4.93"></line></svg></div>';
      document.getElementById('modalStatusText').innerText = '';
      document.getElementById('modelModal').classList.add('show');

      try {
        const rProviders = await apiFetch('/api/providers');
        const pList = await rProviders.json();
        const pData = pList.find(p => p.name === provider);
        const currentModels = new Set(pData ? (pData.models || []) : []);
        const disabledModels = new Set(pData ? (pData.disabled_models || []) : []);

        // 使用 AbortController 控制超时（10秒）
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 10000);

        let availableData = { ok: false, models: [], detail: '请求超时' };
        try {
          const rAvailable = await apiFetch(`/api/providers/${provider}/available-models`, { signal: controller.signal });
          availableData = await rAvailable.json();
        } catch (fetchErr) {
          console.warn(`[${provider}] available-models 请求失败:`, fetchErr.message);
        } finally {
          clearTimeout(timeoutId);
        }

        let allModels = availableData.models || [];
        const fetchOk = availableData.ok;
        const fetchError = !fetchOk && availableData.detail;

        // 获取稳定性数据
        let stabilityMap = {};
        try {
          const rStability = await apiFetch('/api/stability?hours=24');
          const stabilityData = await rStability.json();
          stabilityData.forEach(s => {
            const key = `${s.provider}||${s.model}`;
            stabilityMap[key] = s;
          });
        } catch (e) {
          console.warn('获取稳定性数据失败:', e);
        }

        // 合并已保存的模型
        currentModels.forEach(m => { if (!allModels.includes(m)) allModels.push(m); });
        allModels.sort();

        // 显示状态信息
        if (!fetchOk) {
          document.getElementById('modalStatusText').innerText = fetchError || '远端列表获取失败';
        } else if (allModels.length === 0) {
          document.getElementById('modalStatusText').innerText = '暂无可用模型';
        } else {
          document.getElementById('modalStatusText').innerText = `共加载了 ${allModels.length} 个模型`;
        }

        // 渲染模型列表
        if (allModels.length === 0) {
          document.getElementById('modalModelList').innerHTML = '<div class="empty">暂无模型数据<br><br>请检查上游服务是否在线，或手动添加模型</div>';
        } else {
          // 按排序列排序（默认按名称升序）
          if (modalSortCol === 'size') {
            allModels.sort((a, b) => {
              const sizeA = parseModelSize(a) || 0;
              const sizeB = parseModelSize(b) || 0;
              return modalSortAsc ? sizeA - sizeB : sizeB - sizeA;
            });
          } else if (modalSortCol === 'success') {
            allModels.sort((a, b) => {
              const sa = stabilityMap[`${provider}||${a}`]?.availability || 0;
              const sb = stabilityMap[`${provider}||${b}`]?.availability || 0;
              return modalSortAsc ? sa - sb : sb - sa;
            });
          } else {
            allModels.sort();
          }
          allModelsForModal = allModels;
          currentModelsForModal = currentModels;
          disabledModelsForModal = disabledModels;
          modalStabilityMapForModal = stabilityMap;
          // 确保 modalSortCol 不为 null（默认按名称排序）
          if (!modalSortCol) modalSortCol = 'model';
          renderModalModelTable(allModels, currentModels, disabledModels, stabilityMap, provider);
        }
      } catch (e) {
        if (e.message === 'Unauthorized') return;
        console.error(e);
        // 显示已保存的模型或空状态
        if (currentModels.size === 0) {
          document.getElementById('modalModelList').innerHTML = '<div class="empty" style="color:var(--danger)">获取可用模型列表失败<br><br>请检查上游服务是否在线，或手动添加模型</div>';
          document.getElementById('modalStatusText').innerText = '获取失败';
        } else {
          // 显示已保存的模型
          let allModels = [...currentModels];
          allModels.sort();
          allModelsForModal = allModels;
          currentModelsForModal = currentModels;
          disabledModelsForModal = disabledModels;
          modalStabilityMapForModal = stabilityMap || {};
          renderModalModelTable(allModels, currentModels, disabledModels, stabilityMap || {}, provider);
          document.getElementById('modalStatusText').innerText = `已保存 ${allModels.length} 个模型（远端获取失败）`;
        }
      }
    }

    function renderModalModelTable(allModels, currentModels, disabledModels, stabilityMap, provider) {
      const container = document.getElementById('modalModelList');
      let html = `<table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:var(--mac-window-bg);position:sticky;top:0;z-index:1;border-bottom:1px solid var(--mac-border);">
            <th style="width:32px;padding:6px 8px;text-align:center;border-bottom:1px solid var(--mac-border);"><input type="checkbox" id="modalSelectAll" onchange="toggleModalSelectAll(this.checked)" title="全选/取消全选"></th>
            <th style="padding:6px 8px;text-align:left;font-size:12px;border-bottom:1px solid var(--mac-border);cursor:pointer;user-select:none;" onclick="toggleModalSort('model')">模型名称 <span class="sort-icon" id="modalSortIcon_model">⇅</span></th>
            <th style="padding:6px 8px;text-align:center;font-size:12px;border-bottom:1px solid var(--mac-border);width:80px;cursor:pointer;user-select:none;" onclick="toggleModalSort('size')">模型大小 <span class="sort-icon" id="modalSortIcon_size">⇅</span></th>
            <th style="padding:6px 8px;text-align:center;font-size:12px;border-bottom:1px solid var(--mac-border);width:100px;cursor:pointer;user-select:none;" onclick="toggleModalSort('success')">成功率 <span class="sort-icon" id="modalSortIcon_success">⇅</span></th>
          </tr>
        </thead>
        <tbody id="modalModelTableBody">`;
      
      allModels.forEach(m => {
        const isDisabled = disabledModels.has(m);
        const isChecked = !isDisabled && currentModels.has(m) ? 'checked' : '';
        const size = parseModelSize(m);
        const sizeStr = size ? `${size}B` : '--';
        const key = `${provider}||${m}`;
        const stat = stabilityMap ? stabilityMap[key] : null;
        const checks = stat?.checks || 0;
        const ok = stat?.ok || 0;
        const fail = (stat?.fail || 0) + (stat?.error || 0);
        const availability = stat?.availability || 0;
        
        // 成功率颜色
        let availColor = 'var(--text-secondary)';
        if (availability >= 90) availColor = 'var(--success)';
        else if (availability >= 50) availColor = 'var(--warning)';
        else if (availability > 0) availColor = 'var(--danger)';
        
        const availStr = checks > 0 ? `${availability.toFixed(1)}% (${ok}/${fail})` : '--';
        
        const rowStyle = isDisabled ? 'opacity:0.55;' : '';
        const nameStyle = isDisabled ? 'text-decoration:line-through;color:var(--text-secondary);' : 'font-weight:500;';
        const badgeHtml = isDisabled ? ' <span style="font-size:10px;background:var(--warning);color:#000;padding:1px 4px;border-radius:3px;margin-left:4px;">限额型</span>' : '';
        const cbTitle = isDisabled ? 'title="限额型模型，取消勾选后仍会保留在监控列表中"' : '';
        
        html += `<tr style="border-bottom:1px solid var(--mac-border);${rowStyle}">
          <td style="padding:6px 8px;text-align:center;"><input type="checkbox" class="modal-model-checkbox" value="${m}" ${isChecked} ${cbTitle}></td>
          <td style="padding:6px 8px;${nameStyle}">${m}${badgeHtml}</td>
          <td style="padding:6px 8px;text-align:center;color:var(--text-secondary);">${sizeStr}</td>
          <td style="padding:6px 8px;text-align:center;color:${availColor};font-size:12px;">${availStr}</td>
        </tr>`;
      });
      
      html += '</tbody></table>';
      container.innerHTML = html;
      
      // 更新排序图标
      document.querySelectorAll('#modalModelList .sort-icon').forEach(icon => {
        icon.textContent = '⇅';
        icon.style.opacity = '0.4';
      });
      const activeIcon = document.getElementById(`modalSortIcon_${modalSortCol}`);
      if (activeIcon) {
        activeIcon.textContent = modalSortAsc ? '↑' : '↓';
        activeIcon.style.opacity = '1';
        activeIcon.style.color = 'var(--accent-primary)';
      }
    }

    function toggleModalSort(col) {
      if (modalSortCol === col) {
        modalSortAsc = !modalSortAsc;
      } else {
        modalSortCol = col;
        modalSortAsc = true;
      }
      // 重新排序数据
      const sortedModels = [...allModelsForModal];
      if (modalSortCol === 'size') {
        sortedModels.sort((a, b) => {
          const sizeA = parseModelSize(a) || 0;
          const sizeB = parseModelSize(b) || 0;
          return modalSortAsc ? sizeA - sizeB : sizeB - sizeA;
        });
      } else if (modalSortCol === 'success') {
        const stabilityMap = modalStabilityMapForModal || {};
        sortedModels.sort((a, b) => {
          const keyA = `${currentManagingProvider}||${a}`;
          const keyB = `${currentManagingProvider}||${b}`;
          const sa = stabilityMap[keyA]?.availability || 0;
          const sb = stabilityMap[keyB]?.availability || 0;
          return modalSortAsc ? sa - sb : sb - sa;
        });
      } else {
        sortedModels.sort();
      }
      // 重新渲染
      const selectedModels = new Set([...currentModelsForModal]);
      renderModalModelTable(sortedModels, selectedModels, disabledModelsForModal || new Set(), modalStabilityMapForModal, currentManagingProvider);
    }

    // 全局变量保存当前模态框的模型数据
    let allModelsForModal = [];
    let currentModelsForModal = new Set();
    let disabledModelsForModal = new Set();

    function closeModelManager() {
      document.getElementById('modelModal').classList.remove('show');
      currentManagingProvider = null;
    }

    function filterModalModels() {
      const query = document.getElementById('modalSearchInput').value.toLowerCase();
      document.querySelectorAll('#modalModelTableBody tr').forEach(row => {
        const nameCell = row.querySelector('td:nth-child(2)');
        if (nameCell) {
          const name = nameCell.innerText.toLowerCase();
          row.style.display = name.includes(query) ? '' : 'none';
        }
      });
    }

    function toggleModalSelectAll(checked) {
      document.querySelectorAll('#modalModelTableBody .modal-model-checkbox').forEach(cb => {
        if (cb.closest('tr').style.display !== 'none') cb.checked = checked;
      });
    }

    function selectAllModels(check) {
      document.querySelectorAll('#modalModelTableBody .modal-model-checkbox').forEach(cb => {
        if (cb.closest('tr').style.display !== 'none') cb.checked = check;
      });
    }

    async function saveModelSelection() {
      const checkedModels = [];
      document.querySelectorAll('.modal-model-checkbox').forEach(cb => {
        if (cb.checked) checkedModels.push(cb.value);
      });

      // 计算限额型模型：之前启用但现在取消勾选的
      const disabledList = [];
      allModelsForModal.forEach(m => {
        if (!checkedModels.includes(m) && currentModelsForModal.has(m)) {
          disabledList.push(m);
        }
      });

      const btn = document.querySelector('#modelModal .btn-primary');
      const originalText = btn.innerText;
      btn.innerText = '保存中...';
      btn.disabled = true;
      try {
        const body = { models: checkedModels };
        if (disabledList.length > 0) body.disabled_models = disabledList;
        const res = await apiFetch(`/api/providers/${currentManagingProvider}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        if (res.ok) {
          closeModelManager();
          load();
          checkAll();
        } else {
          alert("保存失败");
        }
      } catch (e) {
        if (e.message !== 'Unauthorized') { console.error(e); alert("请求发生错误"); }
      } finally {
        btn.innerText = originalText;
        btn.disabled = false;
      }
    }

    /* ============ 提供商增删改 ============ */
    let providerEditMode = 'add';
    let providerEditName = null;

    async function openProviderEditor(name) {
      providerEditMode = name ? 'edit' : 'add';
      providerEditName = name || null;
      const titleEl = document.getElementById('providerModalTitle');
      const saveBtn = document.getElementById('providerSaveBtn');
      const nameInput = document.getElementById('pf_name');
      const nameHint = document.getElementById('pf_name_hint');
      const hintEl = document.getElementById('providerModalHint');
      const msgEl = document.getElementById('providerModalMsg');
      msgEl.innerHTML = '';
      msgEl.style.display = 'none';

      if (providerEditMode === 'edit') {
        titleEl.innerText = '编辑提供商';
        saveBtn.innerText = '保存修改';
        hintEl.innerText = '保存后不会改变已选模型列表，仅更新连接信息';
        nameInput.value = name;
        nameInput.disabled = true;
        nameHint.style.display = 'block';
        try {
          const r = await apiFetch('/api/providers');
          const list = await r.json();
          const p = list.find(x => x.name === name) || {};
          document.getElementById('pf_base_url').value = p.base_url || '';
          document.getElementById('pf_free_only').checked = p.free_only !== false;
        } catch (e) { if (e.message !== 'Unauthorized') console.error(e); }
        document.getElementById('pf_api_key').value = '';
        document.getElementById('pf_api_key').placeholder = '留空表示不修改密钥';
      } else {
        titleEl.innerText = '添加提供商';
        saveBtn.innerText = '保存并拉取模型';
        hintEl.innerText = '保存后将自动从上游拉取可用模型列表';
        nameInput.value = '';
        nameInput.disabled = false;
        nameHint.style.display = 'none';
        document.getElementById('pf_base_url').value = '';
        document.getElementById('pf_api_key').value = '';
        document.getElementById('pf_api_key').placeholder = 'sk-...';
        document.getElementById('pf_free_only').checked = true;
      }
      document.getElementById('pf_show_key').checked = false;
      document.getElementById('pf_api_key').type = 'password';
      document.getElementById('providerModal').classList.add('show');
    }

    function closeProviderEditor() {
      document.getElementById('providerModal').classList.remove('show');
      providerEditName = null;
    }

    function showProviderMsg(msg, type) {
      const el = document.getElementById('providerModalMsg');
      const color = type === 'error' ? 'var(--danger)' : 'var(--success)';
      el.innerHTML = '<span style="color:' + color + '">' + msg + '</span>';
      el.style.display = 'block';
    }

    async function submitProviderForm() {
      const name = document.getElementById('pf_name').value.trim();
      const baseUrl = document.getElementById('pf_base_url').value.trim();
      const apiKey = document.getElementById('pf_api_key').value;
      const freeOnly = document.getElementById('pf_free_only').checked;

      if (!name || !baseUrl) { showProviderMsg('名称和 Base URL 不能为空', 'error'); return; }
      if (providerEditMode === 'add' && !apiKey) { showProviderMsg('添加时必须填写 API Key', 'error'); return; }

      const btn = document.getElementById('providerSaveBtn');
      const originalText = btn.innerText;
      btn.innerText = '处理中...';
      btn.disabled = true;

      try {
        let res;
        if (providerEditMode === 'add') {
          res = await apiFetch('/api/providers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, base_url: baseUrl, api_key: apiKey, free_only: freeOnly })
          });
        } else {
          const body = { base_url: baseUrl, free_only: freeOnly };
          if (apiKey) body.api_key = apiKey;
          res = await apiFetch('/api/providers/' + encodeURIComponent(providerEditName), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
          });
        }
        if (res.ok) {
          const wasAdd = providerEditMode === 'add';
          closeProviderEditor();
          showToast(wasAdd ? '提供商已添加，模型列表已自动拉取' : '提供商信息已更新', 'ok');
          await load();
          if (wasAdd) checkAll();
        } else {
          let detail = '';
          try { detail = (await res.json()).detail || ''; } catch (_) { }
          showProviderMsg('操作失败：' + (detail || ('HTTP ' + res.status)), 'error');
        }
      } catch (e) {
        if (e.message !== 'Unauthorized') { console.error(e); showProviderMsg('请求错误：' + e.message, 'error'); }
      } finally {
        btn.innerText = originalText;
        btn.disabled = false;
      }
    }

    async function deleteProvider(name) {
      if (!confirm('确定删除提供商「' + name + '」吗？\n该操作会从 providers.json 移除该提供商及其模型配置，不可恢复。')) return;
      try {
        const res = await apiFetch('/api/providers/' + encodeURIComponent(name), { method: 'DELETE' });
        if (res.ok) { showToast('已删除提供商「' + name + '」', 'ok'); load(); }
        else { showToast('删除失败', 'error'); }
      } catch (e) {
        if (e.message !== 'Unauthorized') { console.error(e); showToast('删除请求错误', 'error'); }
      }
    }

    /* ============ 工具函数 ============ */
    function formatSize(size, modelName) {
      if (!size || size === '--') return '--';
      // 如果名字里已经有大小（如 llama-3.2-90b-vision-instruct），直接显示不加 ~
      if (modelName && /\d+(?:\.\d+)?b/i.test(modelName)) return size.replace('~', '');
      // 否则添加 ~ 前缀表示约等
      return size.startsWith('~') ? size : '~' + size;
    }

    /* ============ 轮询与可见性 ============ */
    let loadTimer = null, detailTimer = null, stabilityTimer = null, usageTimer = null;
    async function loadModelDetails() {
      try {
        const r = await apiFetch('/api/model-details');
        modelDetails = await r.json();
        renderStability();
      } catch (e) { if (e.message !== 'Unauthorized') console.error("Failed to load model details:", e); }
    }
    function startPolling() {
      if (loadTimer) clearInterval(loadTimer);
      if (detailTimer) clearInterval(detailTimer);
      if (stabilityTimer) clearInterval(stabilityTimer);
      if (usageTimer) clearInterval(usageTimer);
      // loadTimer: 拉取提供商配置 + 模型实时健康状态（/api/providers）— 200秒/次
      loadTimer = setInterval(load, 200000);
      // stabilityTimer: 拉取稳定性统计（/api/stability）— 200秒/次
      stabilityTimer = setInterval(loadStability, 200000);
      // usageTimer: 拉取用量统计（/api/usage）— 200秒/次
      usageTimer = setInterval(loadUsage, 200000);
      // 立即执行一次，避免空白
      loadStability();
      loadUsage();
    }
    async function toggleAutoValidate(checked) {
      try {
        const r = await apiFetch('/api/auto-validate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: checked }),
        });
        const data = await r.json();
        const intervalMin = Math.round((data.interval || 1800) / 60);
        showToast(
          data.enabled
            ? `自动验证链接已开启（每 ${intervalMin} 分钟）`
            : '自动验证链接已关闭',
          'ok'
        );
      } catch (e) {
        if (e.message !== 'Unauthorized') console.error("toggleAutoValidate failed:", e);
        showToast('自动验证链接设置失败', 'warn');
      }
    }

    function stopPolling() {
      if (loadTimer) { clearInterval(loadTimer); loadTimer = null; }
      if (detailTimer) { clearInterval(detailTimer); detailTimer = null; }
      if (stabilityTimer) { clearInterval(stabilityTimer); stabilityTimer = null; }
      if (usageTimer) { clearInterval(usageTimer); usageTimer = null; }
    }
    // 切换到当前页签，如果有 token 则立即刷新数据并开始轮询，否则停止轮询
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopPolling();
      else if (getToken()) {
        load();
        startPolling();
        // 立即刷新统计数据，避免切回后看到旧数据
        loadStability();
        loadUsage();
      }
    });

    function switchMainTab(tab) {
      const monitorBtn = document.getElementById('tab-monitor');
      const usageBtn = document.getElementById('tab-usage');
      const providerBtn = document.getElementById('tab-provider');
      const monitorView = document.getElementById('monitor-view');
      const usageView = document.getElementById('usage-view');
      const providerView = document.getElementById('provider-view');

      // Reset all buttons
      [monitorBtn, usageBtn, providerBtn].forEach(btn => {
        btn.classList.remove('active-tab');
        btn.style.background = '';
        btn.style.color = '';
        btn.style.fontWeight = '';
      });

      // Reset all views
      [monitorView, usageView, providerView].forEach(view => {
        view.style.display = 'none';
      });

      if (tab === 'monitor') {
        monitorBtn.classList.add('active-tab');
        monitorView.style.display = 'block';
      } else if (tab === 'usage') {
        usageBtn.classList.add('active-tab');
        usageView.style.display = 'block';
        if (document.getElementById('usageOverview').innerHTML === '') {
          loadUsage(1);
        }
      } else if (tab === 'provider') {
        providerBtn.classList.add('active-tab');
        providerView.style.display = 'block';
        // Load providers if not yet loaded
        if (document.getElementById('providersSection-provider').innerHTML === '') {
          load();
        }
      }
    }

    /* ============ 路由配置 ============ */
    let routersData = {};
    let selectedRouter = '';
    let routerPending = new Set();   // 实时勾选集合：切换筛选时保留勾选状态，保存时提交
    let routerSortCol = '';
    let routerSortAsc = true;

    function toggleRouterSort(col) {
      if (routerSortCol === col) {
        routerSortAsc = !routerSortAsc;
      } else {
        routerSortCol = col;
        routerSortAsc = true;
      }
      // 更新排序图标
      ['model', 'size', 'availability', 'ok', 'latency'].forEach(c => {
        const el = document.getElementById('routerSortIcon_' + c);
        if (el) el.textContent = c === routerSortCol ? (routerSortAsc ? '↑' : '↓') : '⇅';
      });
      renderRouterModelTable();
    }

    async function loadRouterStability() {
      const hours = document.getElementById('routerStabilityHours').value;
      await loadStability(hours, renderRouterModelTable);
      saveFilterState();
    }

    // 清除单个模型的错误标记（点击错误计数）
    async function clearRouterFail(key) {
      try {
        const r = await apiFetch('/api/health-status/clear-fail-counts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: key.split('||')[0] })
        });
        const data = await r.json();
        showToast(`已清除 ${data.cleared} 个错误标记`, 'ok');
        renderRouterModelTable();
      } catch (e) {
        showToast('清除失败: ' + e.message, 'error');
      }
    }

    async function openRouterManager() {
      document.getElementById('routerModal').classList.add('show');
      document.getElementById('newRouterName').value = '';
      selectedRouter = '';
      // 1. 拉取已保存的路由
      try {
        const r = await apiFetch('/api/routers');
        const d = await r.json();
        routersData = d.data || {};
      } catch (e) { if (e.message !== 'Unauthorized') console.error(e); routersData = {}; }
      // 2. 拉取电路断路器状态
      try {
        const r = await apiFetch('/api/health-status');
        const d = await r.json();
        circuitBreakerData = d.circuit_breaker || {};
      } catch (e) { if (e.message !== 'Unauthorized') console.error(e); }
      // 3. 拉取稳定性数据（用于筛选列表）——按路由弹窗当前选择的时间范围加载
      await loadRouterStability();
      // 3. 填充提供商筛选下拉
      const providers = [...new Set(stabilityData.map(d => d.provider))].sort();
      const pf = document.getElementById('routerProviderFilter');
      pf.innerHTML = '<option value="all">全部提供商</option>' + providers.map(p => `<option value="${p}">${p}</option>`).join('');
      // 4. 渲染路由选择器（不自动选中）
      renderRouterList();
      // 5. 默认选中最后一个路由组
      const names = Object.keys(routersData);
      if (names.length > 0) {
        const lastName = names[names.length - 1];
        document.getElementById('routerSelect').value = lastName;
        selectedRouter = lastName;
        initRouterPending();
      } else {
        routerPending = new Set();
      }
      renderRouterModelTable();
    }

    function renderRouterList() {
      const select = document.getElementById('routerSelect');
      const names = Object.keys(routersData);
      const curVal = select.value;
      select.innerHTML = '<option value="">-- 选择路由组编辑 --</option>' +
        names.map(n => `<option value="${n}" ${n === curVal ? 'selected' : ''}>${n}</option>`).join('');
      // 不再自动选中第一个，由调用方显式控制
      renderRouterModelTable();
    }

    function selectRouter(name) {
      selectedRouter = name;
      initRouterPending();
      document.getElementById('routerSelect').value = name;
      renderRouterModelTable();
    }

    async function deleteSelectedRouter() {
      if (!selectedRouter) { showToast('请先选择要删除的路由组', 'warn'); return; }
      if (!confirm('确定删除路由组「' + selectedRouter + '」？此操作不可恢复。')) return;
      const removed = selectedRouter;
      delete routersData[removed];
      selectedRouter = '';
      routerPending = new Set();
      // 重置下拉框
      const select = document.getElementById('routerSelect');
      const names = Object.keys(routersData);
      select.innerHTML = '<option value="">-- 选择路由组编辑 --</option>' +
        names.map(n => `<option value="${n}">${n}</option>`).join('');
      renderRouterModelTable();
      // 立即持久化，避免关闭弹窗后状态丢失
      try {
        const r = await apiFetch('/api/routers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(routersData)
        });
        if (r.ok) showToast(`已删除路由组「${removed}」`, 'ok');
        else showToast('删除失败', 'error');
      } catch (e) {
        if (e.message !== 'Unauthorized') { console.error(e); showToast('删除请求错误', 'error'); }
      }
    }

    async function renameRouter() {
      if (!selectedRouter) { showToast('请先选择要修改的路由组', 'warn'); return; }
      const newName = prompt('请输入新的路由名称：', selectedRouter);
      if (!newName) return;
      const name = newName.trim();
      if (!name) { showToast('路由名称不能为空', 'warn'); return; }
      if (name === selectedRouter) return;
      if (routersData.hasOwnProperty(name)) { showToast('该路由名称已存在', 'warn'); return; }
      // 重命名：保留模型列表
      routersData[name] = routersData[selectedRouter];
      delete routersData[selectedRouter];
      selectedRouter = name;
      // 刷新下拉框并选中新名字
      const select = document.getElementById('routerSelect');
      const names = Object.keys(routersData);
      select.innerHTML = '<option value="">-- 选择路由组编辑 --</option>' +
        names.map(n => `<option value="${n}" ${n === name ? 'selected' : ''}>${n}</option>`).join('');
      select.value = name;
      renderRouterModelTable();
      // 立即持久化
      try {
        const r = await apiFetch('/api/routers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(routersData)
        });
        if (r.ok) showToast(`已重命名为「${name}」`, 'ok');
        else showToast('重命名失败', 'error');
      } catch (e) {
        if (e.message !== 'Unauthorized') { console.error(e); showToast('重命名请求错误', 'error'); }
      }
    }

    async function addRouterGroup() {
      const name = document.getElementById('newRouterName').value.trim();
      if (!name) { showToast('请输入路由名称', 'warn'); return; }
      if (routersData.hasOwnProperty(name)) { showToast('该路由名称已存在', 'warn'); return; }
      routersData[name] = [];
      document.getElementById('newRouterName').value = '';
      // 更新下拉框并选中新路由
      const select = document.getElementById('routerSelect');
      const names = Object.keys(routersData);
      select.innerHTML = '<option value="">-- 选择路由组编辑 --</option>' +
        names.map(n => `<option value="${n}" ${n === name ? 'selected' : ''}>${n}</option>`).join('');
      select.value = name;
      selectedRouter = name;
      routerPending = new Set();
      // 重置筛选条件，便于挑选模型
      document.getElementById('routerSearchFilter').value = '';
      document.getElementById('routerStableFilter').value = 'all';
      document.getElementById('routerHideZeroSuccess').checked = false;
      renderRouterModelTable();
      // 立即持久化，避免关闭弹窗后丢失
      try {
        const r = await apiFetch('/api/routers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(routersData)
        });
        if (r.ok) showToast(`路由组「${name}」已创建，请勾选模型后点保存`, 'ok');
        else showToast('创建失败', 'error');
      } catch (e) {
        if (e.message !== 'Unauthorized') { console.error(e); showToast('创建请求错误', 'error'); }
      }
    }

    function initRouterPending() {
      // 把已保存的裸模型名还原为 provider||model 唯一键（同名多 provider 全展开，互不干扰）
      const savedNames = routersData[selectedRouter] || [];
      routerPending = new Set();
      const nameSet = new Set(savedNames);
      stabilityData.forEach(d => {
        if (nameSet.has(d.model)) routerPending.add(d.provider + '||' + d.model);
      });
      // 供应商列表中找不到的裸名 → 孤儿键（取消勾选即可移除）
      const matched = new Set([...routerPending].map(k => k.split('||')[1]));
      savedNames.forEach(sn => {
        if (!matched.has(sn)) routerPending.add('__orphan__||' + sn);
      });
    }

    function toggleAllRouterModels(checked) {
      if (!selectedRouter) return;
      // 全选/取消全选：同步所有可见行到 routerPending
      document.querySelectorAll('#routerModelTableBody .router-model-cb').forEach(cb => {
        if (checked) routerPending.add(cb.value);
        else routerPending.delete(cb.value);
      });
      renderRouterModelTable();
    }

    function onRouterModelToggle(cb) {
      // 勾选/取消勾选监听：立即更新 routerPending 并重排（已选置顶）
      if (!selectedRouter) return;
      if (cb.checked) routerPending.add(cb.value);
      else routerPending.delete(cb.value);
      renderRouterModelTable();
    }

    function updateRouterSelectedCount() {
      const el = document.getElementById('routerSelectedCount');
      if (!selectedRouter) { el.textContent = ''; return; }
      const checkedInView = document.querySelectorAll('#routerModelTableBody .router-model-cb:checked').length;
      const totalInView = document.querySelectorAll('#routerModelTableBody .router-model-cb').length;
      el.textContent = `路由"${selectedRouter}"：视图内勾选 ${checkedInView} / ${totalInView}  |  已选 ${routerPending.size} 个模型`;
    }

    function copyRouterModels() {
      if (!selectedRouter) { showToast('请先选择路由组', 'warn'); return; }
      navigator.clipboard.writeText(selectedRouter).then(() => {
        showToast(`已复制路由名「${selectedRouter}」`, 'ok');
      });
    }

    function renderRouterModelTable() {
      // 每次渲染前刷新 circuit breaker 数据
      if (document.getElementById('routerModal').classList.contains('show')) {
        apiFetch('/api/health-status').then(r => r.json()).then(d => {
          circuitBreakerData = d.circuit_breaker || {};
          _doRenderRouterTable();
        }).catch(() => _doRenderRouterTable());
      } else {
        _doRenderRouterTable();
      }
    }

    function _doRenderRouterTable() {
      const tbody = document.getElementById('routerModelTableBody');
      const countEl = document.getElementById('routerSelectedCount');
      if (!selectedRouter) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty" style="padding:24px;text-align:center;color:var(--text-secondary);">请先选择或创建一个路由组</td></tr>';
        if (countEl) countEl.textContent = '';
        return;
      }
      if (!stabilityData || !stabilityData.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty" style="padding:24px;text-align:center;color:var(--text-secondary);">稳定性数据加载中…</td></tr>';
        if (countEl) countEl.textContent = '';
        return;
      }

      const filter = document.getElementById('routerStableFilter').value;
      const search = document.getElementById('routerSearchFilter').value.toLowerCase();
      const provider = document.getElementById('routerProviderFilter').value;
      const hideZero = document.getElementById('routerHideZeroSuccess').checked;

      const currentModels = routerPending;   // provider||model 唯一键集合，同名模型互不干扰

      // 候选模型：满足当前筛选条件的
      const passFilter = (d) => {
        if (provider !== 'all' && d.provider !== provider) return false;
        if (search && !d.model.toLowerCase().includes(search)) return false;
        if (hideZero && d.ok === 0) return false;
        if (filter === 'high') return d.availability >= 90;
        if (filter === 'mid') return d.availability >= 50 && d.availability < 90;
        if (filter === 'low') return d.availability < 50;
        return d.checks > 0;
      };

      // 合并：已保存的模型始终显示（保证所见即所得、不丢失），未保存的仅当满足筛选时显示
      const rowMap = new Map(); // key = provider||model
      stabilityData.forEach(d => {
        const key = d.provider + '||' + d.model;
        const isSaved = currentModels.has(key);
        if (isSaved || passFilter(d)) {
          rowMap.set(key, { d, saved: isSaved });
        }
      });

      // 孤儿键（__orphan__||裸名）：供应商列表中已不存在 → 灰色提示行（便于取消勾选删除）
      const orphans = [];
      currentModels.forEach(key => {
        if (key.startsWith('__orphan__')) orphans.push(key.slice(10));
      });

      let list = [...rowMap.values()];

      // 排序：已保存排最前，再按用户选择的列
      list.sort((a, b) => {
        if (a.saved && !b.saved) return -1;
        if (!a.saved && b.saved) return 1;
        if (!routerSortCol) return 0;
        const A = a.d, B = b.d;
        let va, vb;
        if (routerSortCol === 'model') {
          va = A.model.toLowerCase(); vb = B.model.toLowerCase();
        } else if (routerSortCol === 'size') {
          const getSize = (d) => {
            const s = (modelDetails[d.model] || {}).size || (d.model.match(/(\d+(?:\.\d+)?)b/i) ? d.model.match(/(\d+(?:\.\d+)?)b/i)[1] + 'B' : '--');
            const m = s.match(/(\d+(?:\.\d+)?)/);
            return m ? parseFloat(m[1]) : 0;
          };
          va = getSize(A); vb = getSize(B);
        } else if (routerSortCol === 'context_length') {
          va = (modelDetails[A.model] || {}).context_length || A.context_length || A.max_model_len || 0;
          vb = (modelDetails[B.model] || {}).context_length || B.context_length || B.max_model_len || 0;
        } else if (routerSortCol === 'availability') {
          va = A.availability || 0; vb = B.availability || 0;
        } else if (routerSortCol === 'latency') {
          va = A.avg_latency_ms ?? 999999; vb = B.avg_latency_ms ?? 999999;
        } else if (routerSortCol === 'ok') {
          va = A.ok || 0; vb = B.ok || 0;
        }
        const cmp = typeof va === 'string' ? String(va).localeCompare(String(vb)) : (va - vb);
        return routerSortAsc ? cmp : -cmp;
      });

      if (!list.length && !orphans.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty" style="padding:24px;text-align:center;color:var(--text-secondary);">没有匹配的模型，试试调整筛选条件</td></tr>';
        if (countEl) countEl.textContent = '';
        return;
      }

      let html = list.map(({ d, saved }) => {
        const checked = saved ? 'checked' : '';
        const pct = typeof d.availability === 'number' ? d.availability.toFixed(1) : parseFloat(d.availability || 0).toFixed(1);
        const barColor = pct >= 90 ? 'var(--success)' : pct >= 50 ? 'var(--warning)' : 'var(--danger)';
        const avgL = Number.isFinite(Number(d.avg_latency_ms)) ? Math.round(Number(d.avg_latency_ms)) + ' ms' : '--';
        const ok = Number(d.ok) || 0;
        const fail = (Number(d.fail) || 0) + (Number(d.error) || 0);
        const checks = Number(d.checks) || 0;
        const contextLength = (modelDetails[d.model] || {}).context_length || d.context_length || d.max_model_len;
        const contextText = contextLength ? Number(contextLength).toLocaleString() : '--';
        const rowStyle = saved ? ' style="background:rgba(40,205,65,0.06);"' : '';
        const savedBadge = saved ? ' <span style="color:var(--success);font-size:11px;">✓</span>' : '';
        // 无数据/空状态 - 红色徽章
        const badgeClass = checks === 0 ? 'model-provider-badge badge-error' : 'model-provider-badge';
        // 错误标记计数
        const key = `${d.provider}||${d.model}`;
        const cb = circuitBreakerData[key];
        const errCount = cb ? cb.fails : 0;
        const errMark = errCount > 0
          ? `<span style="background:var(--danger);color:#fff;border-radius:4px;padding:1px 6px;font-size:11px;cursor:pointer;" title="点击清除标记" onclick="clearRouterFail('${key}')">${errCount}</span>`
          : '<span style="color:var(--text-secondary);">--</span>';
        return `<tr${rowStyle}>
      <td style="padding:4px 8px;text-align:center;"><input type="checkbox" class="router-model-cb" value="${d.provider}||${d.model}" data-provider="${d.provider}" ${checked} onchange="onRouterModelToggle(this)"></td>
      <td style="padding:4px 8px;font-size:13px;"><span class="${badgeClass}">${d.provider}</span>${d.model}${savedBadge}</td>
      <td style="padding:4px 8px;text-align:center;font-size:12px;color:var(--text-secondary);">${formatSize((modelDetails[d.model] || {}).size || (d.model.match(/(\d+(?:\.\d+)?)b/i) ? d.model.match(/(\d+(?:\.\d+)?)b/i)[1] + 'B' : '--'), d.model)}</td>
      <td style="padding:4px 8px;text-align:center;font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums;">${contextText}</td>
      <td style="padding:4px 8px;text-align:center;font-size:12px;color:${barColor};">${pct}%</td>
      <td style="padding:4px 8px;text-align:center;font-variant-numeric:tabular-nums;font-size:12px;">
        <span style="color:var(--success);font-weight:600">${ok}</span>
        <span style="color:var(--text-secondary);margin:0 4px">/</span>
        <span style="color:var(--danger);font-weight:600">${fail}</span>
      </td>
      <td style="padding:4px 8px;text-align:center;font-size:12px;">
        ${errMark}
      </td>
      <td style="padding:4px 8px;text-align:center;font-size:12px;color:var(--text-secondary);">${avgL}</td>
    </tr>`;
      }).join('');

      // 追加孤儿行：已保存但不在当前供应商列表中
      orphans.forEach(m => {
        html += `<tr style="opacity:0.6;">
      <td style="padding:4px 8px;text-align:center;"><input type="checkbox" class="router-model-cb" value="__orphan__||${m}" checked onchange="onRouterModelToggle(this)" title="该模型不在当前供应商列表中，取消勾选并保存可移除"></td>
      <td colspan="7" style="padding:4px 8px;font-size:13px;color:var(--text-secondary);">⚠️ ${m} <span style="font-size:11px;">（不在当前供应商列表中）</span></td>
    </tr>`;
      });

      tbody.innerHTML = html;
      updateRouterSelectedCount();
    }

    async function saveRouters() {
      if (!selectedRouter) { showToast('请先选择要保存的路由组', 'warn'); return; }
      // 所见即所得：提交实时勾选集合（provider||model 键还原为裸模型名并去重）
      const finalModels = [...new Set([...routerPending].map(k => k.split('||')[1]))];

      // 防误操作：从有内容清空到 0 时二次确认
      const prev = routersData[selectedRouter] || [];
      if (finalModels.length === 0 && prev.length > 0) {
        if (!confirm(`确定要清空路由组「${selectedRouter}」中的所有模型吗？`)) {
          renderRouterModelTable();
          return;
        }
      }

      routersData[selectedRouter] = finalModels;

      try {
        // 先保存路由
        const r = await apiFetch('/api/routers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(routersData)
        });
        if (r.ok) {
          showToast(`已保存「${selectedRouter}」：${finalModels.length} 个模型`, 'ok');
          renderRouterModelTable();
        } else {
          showToast('保存失败', 'error');
        }
      } catch (e) {
        if (e.message !== 'Unauthorized') { console.error(e); showToast('保存请求错误', 'error'); }
      }
    }

    /* ============ 路由表圈选选择 ============ */
    let isSelecting = false;
    let selectionStartX = 0;
    let selectionStartY = 0;
    const selectionBox = document.getElementById('selection-box');

    document.addEventListener('mousedown', event => {
      const routerModal = document.getElementById('routerModal');
      const modelModal = document.getElementById('modelModal');
      const routerTableBody = document.getElementById('routerModelTableBody');
      const modelTableBody = document.getElementById('modalModelTableBody');

      let targetBody = null;
      if (routerModal?.classList.contains('show') && routerTableBody?.contains(event.target)) {
        targetBody = routerTableBody;
      } else if (modelModal?.classList.contains('show') && modelTableBody?.contains(event.target)) {
        targetBody = modelTableBody;
      }

      if (!targetBody) return;
      if (event.target.closest('input, button, a, select')) return;

      isSelecting = true;
      selectionStartX = event.clientX;
      selectionStartY = event.clientY;
      selectionBox.style.left = `${selectionStartX}px`;
      selectionBox.style.top = `${selectionStartY}px`;
      selectionBox.style.width = '0px';
      selectionBox.style.height = '0px';
      selectionBox.style.display = 'block';
      event.preventDefault();
    });

    document.addEventListener('mousemove', event => {
      if (!isSelecting) return;
      const left = Math.min(selectionStartX, event.clientX);
      const top = Math.min(selectionStartY, event.clientY);
      const width = Math.abs(event.clientX - selectionStartX);
      const height = Math.abs(event.clientY - selectionStartY);
      selectionBox.style.left = `${left}px`;
      selectionBox.style.top = `${top}px`;
      selectionBox.style.width = `${width}px`;
      selectionBox.style.height = `${height}px`;

      // 1. 处理路由表
      const routerTableBody = document.getElementById('routerModelTableBody');
      if (routerTableBody) {
        document.querySelectorAll('#routerModelTableBody tr').forEach(row => {
          const checkbox = row.querySelector('.router-model-cb');
          if (!checkbox) return;
          const rect = row.getBoundingClientRect();
          const overlaps = rect.left < left + width && rect.right > left &&
            rect.top < top + height && rect.bottom > top;
          if (overlaps) {
            checkbox.checked = true;
            checkbox.dispatchEvent(new Event('change', { bubbles: true }));
          }
        });
      }

      // 2. 处理模型管理表
      const modelTableBody = document.getElementById('modalModelTableBody');
      if (modelTableBody) {
        document.querySelectorAll('#modalModelTableBody tr').forEach(row => {
          const checkbox = row.querySelector('.modal-model-checkbox');
          if (!checkbox) return;
          const rect = row.getBoundingClientRect();
          const overlaps = rect.left < left + width && rect.right > left &&
            rect.top < top + height && rect.bottom > top;
          if (overlaps) {
            checkbox.checked = true;
            checkbox.dispatchEvent(new Event('change', { bubbles: true }));
          }
        });
      }
    });

    document.addEventListener('mouseup', () => {
      if (!isSelecting) return;
      isSelecting = false;
      selectionBox.style.display = 'none';
    });

    /* ============ 上下文长度 inline 编辑 ============ */
    document.addEventListener('click', e => {
      const cell = e.target.closest('.ctx-cell');
      if (!cell || cell.querySelector('input')) return;
      const model = cell.dataset.model;
      const raw = cell.textContent.replace(/,/g, '').trim();
      const input = document.createElement('input');
      input.type = 'number';
      input.value = raw === '--' ? '' : raw;
      input.style.cssText = 'width:100px;padding:2px 4px;font-size:12px;font-family:ui-monospace,monospace;border:1px solid var(--accent-primary);border-radius:4px;background:#fff;color:var(--text-primary);outline:none;box-sizing:border-box;';
      cell.textContent = '';
      cell.appendChild(input);
      input.focus(); input.select();
      let done = false;
      const finish = async (save) => {
        if (done) return; done = true;
        if (!save) { cell.textContent = raw; return; }
        const v = parseInt(input.value, 10);
        if (!v || v <= 0) { cell.textContent = raw; return; }
        try {
          const r = await apiFetch('/api/context-limits', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: model, context_length: v })
          });
          if (r.ok) {
            showToast(`已更新 ${model} = ${v}`, 'ok');
            if (modelDetails[model]) modelDetails[model].context_length = v;
            else modelDetails[model] = { context_length: v };
            cell.textContent = String(v);
            loadStability();
          }
          else { showToast('更新失败', 'error'); cell.textContent = raw; }
        } catch (e) {
          if (e.message !== 'Unauthorized') { console.error(e); showToast('请求错误', 'error'); }
          cell.textContent = raw;
        }
      };
      input.addEventListener('blur', () => finish(true));
      input.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
        if (ev.key === 'Escape') { ev.preventDefault(); finish(false); }
      });
    });

    /* ============ 启动 ============ */
    function startApp() {
      loadFilterState();
      // 从后端恢复自动验证开关状态
      restoreAutoValidate();
      loadModelDetails(); load(); startPolling();
    }
    async function restoreAutoValidate() {
      try {
        const r = await apiFetch('/api/auto-validate');
        const data = await r.json();
        const sw = document.getElementById('autoValidateSwitch');
        if (sw) sw.checked = !!data.enabled;
      } catch (e) {
        if (e.message !== 'Unauthorized') console.error("restoreAutoValidate failed:", e);
      }
    }
    startApp();
  