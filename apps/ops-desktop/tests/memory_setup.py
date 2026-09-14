"""Real Tauri memory-provider forms; no credential entry or provider request."""
import base64
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT/'artifacts/credential-form-fix-2026-09-12/desktop'
API = 'http://127.0.0.1:4459'


def call(method, path, data=None):
    request = urllib.request.Request(API+path, method=method,
        headers={'Content-Type':'application/json'},
        data=None if data is None else json.dumps(data).encode())
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)['value']


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DISPLAY=':98', GDK_BACKEND='x11', WEBKIT_DISABLE_DMABUF_RENDERER='1')
    env.pop('WAYLAND_DISPLAY', None)
    xvfb = subprocess.Popen(['Xvfb', ':98', '-screen', '0', '1440x1000x24', '-nolisten', 'tcp'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    session = None
    driver = None
    try:
        time.sleep(1)
        driver = subprocess.Popen(['/home/ops-user/.cargo/bin/tauri-driver', '--port', '4459', '--native-port', '4460'],
            env=env, stdout=(OUT/'driver.log').open('w'), stderr=subprocess.STDOUT)
        for _ in range(40):
            try:
                call('GET', '/status')
                break
            except Exception:
                time.sleep(.3)
        result = call('POST', '/session', {'capabilities': {'alwaysMatch': {
            'browserName':'wry', 'tauri:options': {
                'application':str(ROOT/'apps/ops-desktop/src-tauri/target/release/ops-desktop')}}}})
        session = result['sessionId']
        prefix = '/session/'+session
        def js(script):
            return call('POST', prefix+'/execute/sync', {'script':script, 'args':[]})
        time.sleep(4)
        for handle in call('GET', prefix+'/window/handles'):
            call('POST', prefix+'/window', {'handle':handle})
            if '/atlas/' in call('GET', prefix+'/url'):
                break
        for _ in range(50):
            if js('return !!document.querySelector("#tab-settings") && document.querySelectorAll(".model-card").length>0'):
                break
            time.sleep(.3)
        assert js('return document.querySelectorAll(".system-default-card.system-recommended").length') == 3
        js("document.querySelector('.system-defaults [data-configure-account=\"jina\"]').click()")
        assert js('return document.querySelector("#credential-dialog").open')
        js('document.querySelector(".credential-dialog-close").click()')
        (OUT/'recommended-models.png').write_bytes(base64.b64decode(call('GET', prefix+'/screenshot')))
        js('document.querySelector("#tab-settings").click()')
        assert js('return !document.querySelector("#panel-settings").hidden')
        assert js('return document.querySelector("#memory-setup-title").textContent') == 'Codex, Claude et Hermes'
        for account in ['deepseek', 'jina']:
            js('document.querySelector(\'[data-configure-account="'+account+'"]\').click()')
            assert js('return document.querySelector("#credential-dialog").open')
            assert js('return document.querySelector("#credential-account-id").textContent') == account
            assert js('return document.querySelector("#credential-openbao-password").type') == 'password'
            assert js('return document.querySelector("#credential-api-key").type') == 'password'
            assert js('return document.querySelector("#credential-save").disabled')
            # Synthetic values only; never submit a credential during this test.
            assert js('return typeof window.AtlasOnboarding.validateProviderCredential') == 'function'
            assert js('return typeof window.AtlasOnboarding.init') == 'undefined'
            js("const p=document.querySelector('#credential-openbao-password'); const k=document.querySelector('#credential-api-key'); p.value='Synthetic-test-password-123'; k.value='synthetic-api-token-123'; p.dispatchEvent(new Event('input',{bubbles:true})); k.dispatchEvent(new Event('input',{bubbles:true}));")
            assert js('return document.querySelector("#credential-save").disabled') is False
            js("const k=document.querySelector('#credential-api-key'); k.value='invalid key with spaces'; k.dispatchEvent(new Event('input',{bubbles:true}));")
            assert js('return document.querySelector("#credential-save").disabled') is True
            js("for (const id of ['credential-openbao-password','credential-api-key']) {const n=document.getElementById(id);n.value='';n.dispatchEvent(new Event('input',{bubbles:true}));}")
            (OUT/(account+'-empty-form.png')).write_bytes(base64.b64decode(call('GET', prefix+'/screenshot')))
            js('document.querySelector(".credential-dialog-close").click()')
            # Empty input is rejected before launching the secret helper.
            value = call('POST', prefix+'/execute/async', {'script':
                'const done=arguments[arguments.length-1]; window.__TAURI__.core.invoke("save_provider_credential",'
                +json.dumps({'providerAccountId':account,'openBaoPassword':'','apiKey':''})
                +').then(()=>done("unexpected-success")).catch(e=>done(e.code));', 'args':[]})
            assert value == 'credential_invalid', value
        report = {'native_forms': ['deepseek','jina'], 'masked_inputs': True, 'recommended_cards': 3, 'valid_input_enables_save': True, 'invalid_input_disables_save': True,
                  'empty_submission_rejected': True, 'provider_calls': False, 'credentials_written': False}
        (OUT/'acceptance.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report))
    finally:
        if session:
            try:
                call('DELETE', '/session/'+session)
            except Exception:
                pass
        if driver:
            driver.terminate()
            driver.wait(timeout=10)
        xvfb.terminate()
        xvfb.wait(timeout=10)


if __name__ == '__main__':
    main()
