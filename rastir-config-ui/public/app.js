// Rastir Config UI

const API_BASE_URL = 'http://localhost:8080';

// ── Toggle helpers ──────────────────────────────────────────────────────────

function initToggle(id) {
    const toggle = document.getElementById(`${id}-toggle`);
    const hidden = document.getElementById(id);
    const label  = document.getElementById(`${id}-label`);

    function sync() {
        hidden.value = toggle.checked ? 'true' : 'false';
        label.textContent = toggle.checked ? 'Enabled' : 'Disabled';
    }

    toggle.addEventListener('change', sync);
    sync();
}

function setToggle(id, value) {
    const toggle = document.getElementById(`${id}-toggle`);
    const hidden = document.getElementById(id);
    const label  = document.getElementById(`${id}-label`);
    toggle.checked = !!value;
    hidden.value   = value ? 'true' : 'false';
    label.textContent = value ? 'Enabled' : 'Disabled';
}

// ── Sampling slider ─────────────────────────────────────────────────────────

function initSlider() {
    const slider  = document.getElementById('sampling-rate-slider');
    const number  = document.getElementById('sampling-rate');
    const display = document.getElementById('sampling-rate-display');

    function syncFromSlider() {
        const v = parseFloat(slider.value).toFixed(2);
        number.value  = v;
        display.textContent = v;
    }

    function syncFromNumber() {
        const v = Math.min(1, Math.max(0, parseFloat(number.value) || 0));
        slider.value  = v;
        display.textContent = v.toFixed(2);
    }

    slider.addEventListener('input', syncFromSlider);
    number.addEventListener('input', syncFromNumber);
}

function setSamplingRate(value) {
    const v = parseFloat(value).toFixed(2);
    document.getElementById('sampling-rate-slider').value = v;
    document.getElementById('sampling-rate').value = v;
    document.getElementById('sampling-rate-display').textContent = v;
}

// ── Sidebar active nav ──────────────────────────────────────────────────────

function initNav() {
    const items = document.querySelectorAll('.nav-item');
    items.forEach(item => {
        item.addEventListener('click', () => {
            items.forEach(i => i.classList.remove('active'));
            item.classList.add('active');
        });
    });
}

// ── Server status ───────────────────────────────────────────────────────────

function setServerStatus(online) {
    const dot  = document.getElementById('server-dot');
    const text = document.getElementById('server-status-text');
    if (online) {
        dot.style.background = 'var(--success)';
        dot.style.boxShadow  = '0 0 6px var(--success)';
        text.textContent = 'Server online';
    } else {
        dot.style.background = 'var(--danger)';
        dot.style.boxShadow  = '0 0 6px var(--danger)';
        text.textContent = 'Server offline';
    }
}

// ── Toast ───────────────────────────────────────────────────────────────────

let toastTimer;
function showStatus(message, type = 'info') {
    const el = document.getElementById('toast');
    el.textContent = message;
    el.className = type;
    el.style.display = 'block';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.style.display = 'none'; }, 4000);
}

// ── Populate ────────────────────────────────────────────────────────────────

function populateForm(config) {
    setSamplingRate(config.sampling.rate);

    setToggle('evaluation-enabled', config.evaluation.enabled);
    document.getElementById('evaluation-queue-size').value = config.evaluation.queue_size;

    setToggle('rate-limit-enabled', config.rate_limit.enabled);
    document.getElementById('rate-limit-per-ip-rpm').value = config.rate_limit.per_ip_rpm;
    document.getElementById('rate-limit-per-service-rpm').value = config.rate_limit.per_service_rpm;

    document.getElementById('backpressure-soft-limit-pct').value = config.backpressure.soft_limit_pct;
    document.getElementById('backpressure-hard-limit-pct').value = config.backpressure.hard_limit_pct;

    document.getElementById('logging-level').value = config.logging.level;
    setToggle('logging-structured', config.logging.structured);

    document.getElementById('limits-max-traces').value = config.limits.max_traces;
    document.getElementById('limits-max-queue-size').value = config.limits.max_queue_size;

    setToggle('sre-enabled', config.sre.enabled);
    document.getElementById('sre-default-slo-error-rate').value = config.sre.default_slo_error_rate;
    document.getElementById('sre-default-cost-budget-usd').value = config.sre.default_cost_budget_usd;
}

function updateSourceBadges() {
    const ids = [
        'sampling-rate', 'evaluation-enabled', 'evaluation-queue-size',
        'rate-limit-enabled', 'rate-limit-per-ip-rpm', 'rate-limit-per-service-rpm',
        'backpressure-soft-limit-pct', 'backpressure-hard-limit-pct',
        'logging-level', 'logging-structured',
        'limits-max-traces', 'limits-max-queue-size',
        'sre-enabled', 'sre-default-slo-error-rate', 'sre-default-cost-budget-usd',
    ];
    ids.forEach(id => {
        const badge = document.getElementById(`${id}-source`);
        if (badge) { badge.className = 'badge source-runtime'; badge.textContent = 'runtime'; }
    });
}

// ── Load ────────────────────────────────────────────────────────────────────

async function loadConfig() {
    try {
        const res = await fetch(`${API_BASE_URL}/config`);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const config = await res.json();
        populateForm(config);
        updateSourceBadges();
        setServerStatus(true);
    } catch (err) {
        setServerStatus(false);
        showStatus('Failed to load config: ' + err.message, 'error');
    }
}

// ── Build payload ───────────────────────────────────────────────────────────

function buildConfig() {
    return {
        sampling: {
            rate: parseFloat(document.getElementById('sampling-rate').value),
        },
        evaluation: {
            enabled:    document.getElementById('evaluation-enabled').value === 'true',
            queue_size: parseInt(document.getElementById('evaluation-queue-size').value),
        },
        rate_limit: {
            enabled:         document.getElementById('rate-limit-enabled').value === 'true',
            per_ip_rpm:      parseInt(document.getElementById('rate-limit-per-ip-rpm').value),
            per_service_rpm: parseInt(document.getElementById('rate-limit-per-service-rpm').value),
        },
        backpressure: {
            soft_limit_pct: parseFloat(document.getElementById('backpressure-soft-limit-pct').value),
            hard_limit_pct: parseFloat(document.getElementById('backpressure-hard-limit-pct').value),
        },
        logging: {
            level:      document.getElementById('logging-level').value,
            structured: document.getElementById('logging-structured').value === 'true',
        },
        limits: {
            max_traces:     parseInt(document.getElementById('limits-max-traces').value),
            max_queue_size: parseInt(document.getElementById('limits-max-queue-size').value),
        },
        sre: {
            enabled:                  document.getElementById('sre-enabled').value === 'true',
            default_slo_error_rate:   parseFloat(document.getElementById('sre-default-slo-error-rate').value),
            default_cost_budget_usd:  parseFloat(document.getElementById('sre-default-cost-budget-usd').value),
        },
    };
}

// ── Save ────────────────────────────────────────────────────────────────────

async function saveConfig() {
    const btn = document.getElementById('save-btn');
    btn.disabled = true;
    try {
        const res = await fetch(`${API_BASE_URL}/config`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(buildConfig()),
        });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Save failed');
        }
        showStatus('Configuration saved successfully', 'success');
        setTimeout(loadConfig, 800);
    } catch (err) {
        showStatus('Save failed: ' + err.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

// ── Reload ──────────────────────────────────────────────────────────────────

async function reloadConfig() {
    const btn = document.getElementById('reload-btn');
    btn.disabled = true;
    try {
        const res = await fetch(`${API_BASE_URL}/config/reload`, { method: 'POST' });
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Reload failed');
        }
        showStatus('Configuration reloaded from file', 'success');
        setTimeout(loadConfig, 800);
    } catch (err) {
        showStatus('Reload failed: ' + err.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

// ── Init ────────────────────────────────────────────────────────────────────

initSlider();
initToggle('evaluation-enabled');
initToggle('rate-limit-enabled');
initToggle('logging-structured');
initToggle('sre-enabled');
initNav();

document.getElementById('save-btn').addEventListener('click', saveConfig);
document.getElementById('reload-btn').addEventListener('click', reloadConfig);

loadConfig();
