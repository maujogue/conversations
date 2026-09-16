import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).parent
REPO = ROOT / 'repo'
BACKEND = REPO / 'src/backend'
FRONTEND = REPO / 'src/frontend'
PYTHON = ROOT / 'backend-venv/bin/python'
NODE = ROOT / 'tools/node/bin/node'
YARN = ROOT / 'tools/node_modules/yarn/bin/yarn.js'
env = os.environ.copy()
env.update({line.split('=', 1)[0]: line.split('=', 1)[1] for line in (REPO / 'env.d/test').read_text().splitlines() if line and not line.startswith('#')})
env.update(
    PATH=f'{ROOT}/tools/node/bin:{ROOT}/tools/node_modules/.bin:{ROOT}/backend-venv/bin:' + os.environ['PATH'],
    XDG_CACHE_HOME=str(ROOT / 'cache'),
    npm_config_cache=str(ROOT / 'cache/npm'),
    npm_config_userconfig=str(ROOT / 'tools/user.npmrc'),
    npm_config_globalconfig=str(ROOT / 'tools/global.npmrc'),
    YARN_CACHE_FOLDER=str(ROOT / 'cache/yarn'),
    PLAYWRIGHT_BROWSERS_PATH=str(ROOT / 'browsers'),
    PYTHONDONTWRITEBYTECODE='1',
    PYTHONPATH=str(BACKEND),
    DATA_DIR=str(ROOT / 'data'),
    DJANGO_SETTINGS_MODULE='conversations.settings',
    DJANGO_CONFIGURATION='Test',
    DB_HOST='127.0.0.1', DB_PORT='25432',
    AWS_S3_ENDPOINT_URL='http://127.0.0.1:29000',
    AWS_S3_DOMAIN_REPLACE='http://localhost:9000',
    LANGFUSE_ENABLED='false',
    HF_HUB_DISABLE_TELEMETRY='1',
    TIKTOKEN_CACHE_DIR=str(ROOT / 'cache/tiktoken'),
    MPLCONFIGDIR=str(ROOT / 'cache/matplotlib'),
    PYLINTHOME=str(ROOT / 'cache/pylint'),
    UV_CACHE_DIR=str(ROOT / 'cache/uv'),
)

def run(name, args, cwd=BACKEND):
    log = ROOT / 'logs' / f'{name}.log'
    print(f'Running {name}; log: {log}', flush=True)
    with log.open('w') as output:
        result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    print(log.read_text(errors='replace')[-7000:], flush=True)
    print(f'{name} exit={result.returncode}', flush=True)
    return result.returncode

if __name__ == '__main__':
    action = sys.argv[1]
    if action.startswith('live-'):
        env.update(PYTHONPATH=str(ROOT) + ':' + str(BACKEND), DJANGO_SETTINGS_MODULE='live_settings', DJANGO_CONFIGURATION='Review', SESSION_ENGINE='django.contrib.sessions.backends.db', FEATURE_FLAG_ARENA='ENABLED', LLM_CONFIGURATION_FILE_PATH=str(ROOT / 'live-models.json'), PYTHON_SERVER_MODE='async', AWS_S3_DOMAIN_REPLACE='http://127.0.0.1:29000')
    if action == 'live-probes':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'live_probes.py']))
    if action == 'live-real-prepare':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'prepare_real.py']))
    if action == 'live-real-start':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'start_real.py']))
    if action == 'live-observe':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'finish_observations.py']))
    if action == 'live-prepare':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'prepare_live.py']))
    if action == 'live-start':
        env.update(VITE_API_ORIGIN='http://127.0.0.1:28071')
        processes = {
            'mock-llm': ([PYTHON, '-B', ROOT / 'mock_llm.py'], ROOT),
            'backend': ([PYTHON, '-B', '-m', 'uvicorn', 'conversations.asgi:application', '--host', '127.0.0.1', '--port', '28071', '--lifespan', 'off'], BACKEND),
            'frontend': ([NODE, FRONTEND / 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '23000'], FRONTEND / 'apps/conversations'),
        }
        for name, (args, cwd) in processes.items():
            pidfile = ROOT / (name + '.pid')
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), 0)
                    print(name, 'already running')
                    continue
                except ProcessLookupError:
                    pass
            output = (ROOT / 'logs' / (name + '-server.log')).open('w')
            child = subprocess.Popen([str(x) for x in args], cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            (ROOT / (name + '.pid')).write_text(str(child.pid))
            print(name, child.pid)
    if action == 'bundle':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'bundle.py']))
    if action == 'install-front':
        sys.exit(run(action, [NODE, YARN, 'install', '--frozen-lockfile', '--non-interactive'], FRONTEND))
    if action == 'real-models':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'real_models.py']))
    if action == 'real-benchmark':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'real_benchmark.py']))
    if action == 'browser-extra':
        env.update(LD_LIBRARY_PATH=str(ROOT / 'tools/browser-libs/usr/lib64'))
        sys.exit(run(action, [NODE, ROOT / 'browser_extra.cjs'], FRONTEND))
    if action == 'browser-real':
        env.update(LD_LIBRARY_PATH=str(ROOT / 'tools/browser-libs/usr/lib64'))
        sys.exit(run(action, [NODE, ROOT / 'browser_real.cjs'], FRONTEND))
    if action == 'browser-race':
        env.update(LD_LIBRARY_PATH=str(ROOT / 'tools/browser-libs/usr/lib64'))
        sys.exit(run(action, [NODE, ROOT / 'browser_race.cjs'], FRONTEND))
    if action == 'browser-run':
        env.update(LD_LIBRARY_PATH=str(ROOT / 'tools/browser-libs/usr/lib64'))
        sys.exit(run(action, [NODE, ROOT / 'browser_review.cjs'], FRONTEND))
    if action == 'backend-retest':
        import xml.etree.ElementTree as ET
        cases = []
        for test in ET.parse(ROOT / 'artifacts/backend-all.xml').iter('testcase'):
            if test.find('failure') is not None:
                cases.append(test.attrib['classname'].replace('.', '/') + '.py::' + test.attrib['name'])
        sys.exit(run(action, [PYTHON, '-B', '-m', 'pytest', '-o', 'env_files=' + str(REPO / 'env.d/test'), '-o', 'addopts=', '-p', 'no:icdiff', '-vv', '--tb=short', '--junitxml=' + str(ROOT / 'artifacts/backend-retest.xml'), *cases]))
    if action == 'build-mail':
        sys.exit(run(action, [NODE, YARN, 'build'], REPO / 'src/mail'))
    if action == 'backend-test':
        sys.exit(run('backend-' + sys.argv[2], [PYTHON, '-B', '-m', 'pytest', '-o', 'env_files=' + str(REPO / 'env.d/test'), '-o', 'addopts=', '--junitxml=' + str(ROOT / 'artifacts' / ('backend-' + sys.argv[2] + '.xml')), *sys.argv[3:]]))
    if action == 'frontend-test':
        sys.exit(run(action, [NODE, FRONTEND / 'node_modules/vitest/vitest.mjs', 'run', '--maxWorkers=4', '--reporter=default', '--reporter=json', '--outputFile.json=' + str(ROOT / 'artifacts/frontend-tests.json')], FRONTEND / 'apps/conversations'))
    if action == 'frontend-lint':
        sys.exit(run(action, [NODE, YARN, 'lint'], FRONTEND / 'apps/conversations'))
    if action == 'frontend-build':
        sys.exit(run(action, [NODE, YARN, 'build'], FRONTEND / 'apps/conversations'))
    if action == 'browser-install':
        sys.exit(run(action, [NODE, FRONTEND / 'node_modules/playwright/cli.js', 'install', 'chromium', '--only-shell'], FRONTEND))
    if action == 'manage':
        sys.exit(run('manage-' + sys.argv[2], [PYTHON, '-B', BACKEND / 'manage.py', *sys.argv[2:]]))
    if action == 'postgres-start':
        pg = ROOT / 'tools/postgres/pgserver/pginstall/bin'
        data = ROOT / 'data/postgres'
        sockets = ROOT / 'data/sockets'
        sockets.mkdir(parents=True, exist_ok=True)
        if not (data / 'PG_VERSION').exists():
            if run('postgres-init', [pg / 'initdb', '-D', data, '-U', 'dinum', '-A', 'trust', '--encoding=UTF8', '--no-locale']):
                sys.exit(1)
        code = run(action, [pg / 'pg_ctl', '-D', data, '-l', ROOT / 'logs/postgres-server.log', '-o', f'-h 127.0.0.1 -p 25432 -k {sockets}', '-w', 'start'])
        if code == 0:
            code = run('postgres-create', [pg / 'createdb', '-h', '127.0.0.1', '-p', '25432', '-U', 'dinum', 'conversations'])
        sys.exit(code)
    if action == 'tokenizer-prepare':
        sys.exit(run(action, [PYTHON, '-B', '-c', 'import tiktoken; print([(n,tiktoken.get_encoding(n).n_vocab) for n in ("cl100k_base","o200k_base")])']))
    if action == 'minio-start':
        env.update(MINIO_ROOT_USER='conversations', MINIO_ROOT_PASSWORD='password', MINIO_BROWSER='off', MINIO_UPDATE='off')
        output = (ROOT / 'logs/minio-server.log').open('w')
        child = subprocess.Popen([ROOT / 'tools/minio', 'server', '--address', '127.0.0.1:29000', '--console-address', '127.0.0.1:29001', str(ROOT / 'data/minio')], env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        (ROOT / 'minio.pid').write_text(str(child.pid))
        print(f'MinIO started with pid {child.pid}')
    if action == 'storage-prepare':
        sys.exit(run(action, [PYTHON, '-B', ROOT / 'tools/prepare_storage.py']))
    if action == 'compile-translations':
        sys.exit(run(action, [PYTHON, '-B', '-c', 'from pathlib import Path; from babel.messages.pofile import read_po; from babel.messages.mofile import write_mo; files=list(Path("locale").rglob("*.po")); [(write_mo(p.with_suffix(".mo").open("wb"), read_po(p.open("rb")))) for p in files]; print("Compiled", len(files), "catalogs in temporary copy")']))
    if action == 'browser-libs-download':
        target = ROOT / 'tools/rpms'
        target.mkdir(exist_ok=True)
        sys.exit(run(action, ['dnf', '--setopt=cachedir=' + str(ROOT / 'cache/dnf'), '--setopt=system_cachedir=' + str(ROOT / 'cache/dnf-system'), '--setopt=logdir=' + str(ROOT / 'logs/dnf'), '--setopt=persistdir=' + str(ROOT / 'cache/dnf-persist'), 'download', '--resolve', '--arch=x86_64', '--destdir=' + str(target), 'at-spi2-atk', 'at-spi2-core', 'atk', 'libXcomposite', 'libXdamage', 'libXfixes', 'libXrandr', 'alsa-lib'], ROOT))
