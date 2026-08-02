/**
 * Hermes Update Card widget for MoiraiCore Dashboard.
 * Reads workspace/hermes-updates/latest.json and renders a status card.
 * Call loadHermesUpdates() from the workspace widget loader.
 */
async function loadHermesUpdates() {
  try {
    const resp = await fetch('/workspace/hermes-updates/latest.json?t=' + Date.now());
    if (!resp.ok) {
      console.log('[hermes-widget] No update file found yet (first check pending)');
      return;
    }
    const data = await resp.json();
    renderHermesCard(data);
  } catch (e) {
    console.log('[hermes-widget] Hermes update data not available:', e.message);
  }
}

function renderHermesCard(data) {
  // Find or create the update card in the dashboard
  let card = document.getElementById('hermes-update-card');
  if (!card) {
    card = document.createElement('div');
    card.id = 'hermes-update-card';
    card.className = 'workspace-widget';
    // Insert into the dashboard workspace area
    const container = document.getElementById('workspace-widgets') || document.body;
    container.prepend(card);
  }

  const severityColors = {
    critical: '#ef4444',
    high: '#f59e0b',
    medium: '#3b82f6',
    info: '#6b7280',
  };
  const color = severityColors[data.severity] || severityColors.info;

  let capsHtml = '';
  if (data.capabilities && data.capabilities.length > 0) {
    capsHtml = data.capabilities.map(c =>
      `<li class="hermes-cap">
        <span class="sev-badge" style="background:${color};color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;margin-right:6px;">
          ${c.severity.toUpperCase()}
        </span>
        ${escapeHtml(c.description)}
      </li>`
    ).join('');
    capsHtml = `<ul class="hermes-cap-list">${capsHtml}</ul>`;
  }

  card.innerHTML = `
    <div class="widget-header" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <h3 style="margin:0;font-size:14px;">Hermes Agent Update</h3>
      <span class="version-badge" style="background:${color};color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;">
        v${data.version}
      </span>
    </div>
    <p style="margin:0 0 8px 0;font-size:12px;color:#9ca3af;">
      ${escapeHtml(data.summary)}
      ${data.checked_at ? `<br><small>Checked: ${new Date(data.checked_at).toLocaleString()}</small>` : ''}
    </p>
    ${capsHtml}
    ${data.action_required
      ? `<div style="margin-top:8px;padding:6px 10px;background:#ef444420;border-left:3px solid #ef4444;font-size:12px;">
          ⚠️ Action required — review before integrating into MoiraiCore
         </div>`
      : ''}
    <div style="margin-top:8px;">
      <a href="${escapeHtml(data.changelog_url)}" target="_blank" rel="noopener"
         style="font-size:11px;color:#60a5fa;text-decoration:none;">
        View changelog →
      </a>
    </div>
  `;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str || '';
  return div.innerHTML;
}
