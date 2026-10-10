"""Fill only the native campus login form, never an external SSO provider."""

import json
from urllib.parse import urlparse


def native_login_script(base_url, user, password):
    origin = urlparse(base_url)
    if origin.scheme != "https" or not origin.netloc or origin.username or origin.password:
        raise ValueError("Se requiere el origen HTTPS de Blackboard.")
    campus = f"{origin.scheme}://{origin.netloc}"
    values = json.dumps({"origin": campus, "user": user, "password": password})
    return """(() => {
        const saved = VALUES;
        if (location.origin !== saved.origin) return 'external';
        const user = document.querySelector('input#user_id, input[name="user_id"]');
        const pass = document.querySelector('input#password, input[name="password"]');
        if (!user || !pass || !user.form || user.form !== pass.form) return 'missing';
        const form = user.form;
        if (new URL(form.action || location.href, location.href).origin !== saved.origin)
            return 'external';
        if (form.method.toLowerCase() !== 'post') return 'external';
        if (window.__zoom2txtSubmitted) return 'submitted';
        window.__zoom2txtSubmitted = true;
        for (const [input, value] of [[user, saved.user], [pass, saved.password]]) {
            input.value = value;
            input.dispatchEvent(new Event('input', {bubbles:true}));
            input.dispatchEvent(new Event('change', {bubbles:true}));
        }
        // Clicking the native button preserves its name/value and campus handlers.
        const submit = form.querySelector('input[type="submit"], button[type="submit"]');
        if (submit) submit.click();
        else form.requestSubmit();
        return 'submitted';
    })()""".replace("VALUES", values)
