"""Windows local provider launcher. Credentials and data stay under this project."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.request

BASE = Path(__file__).resolve().parent
RUNTIME = BASE / 'runtime'

def initialize():
    RUNTIME.mkdir(exist_ok=True)
    key = RUNTIME / 'music-local.json'
    if not key.exists():
        with key.open('x', encoding='utf-8') as f:
            json.dump({'token': secrets.token_urlsafe(36)}, f)
    admin = RUNTIME / 'songloft-local.env'
    if not admin.exists():
        with admin.open('x', encoding='utf-8') as f:
            f.write('SONGLOFT_ADMIN_PASSWORD=' + secrets.token_urlsafe(27) + '\n')
    for name in ('local-data', 'songloft-local-data', 'songloft-local-music'):
        (BASE / name).mkdir(exist_ok=True)

def serve():
    initialize()
    key = json.loads((RUNTIME / 'music-local.json').read_text(encoding='utf-8'))['token']
    os.environ.update(HOME_MUSIC_TOKEN=key, HOME_MUSIC_PUBLIC_URL='http://127.0.0.1:58092',
                      HOME_MUSIC_BIND_HOST='127.0.0.1', HOME_MUSIC_PORT='58092',
                      HOME_MUSIC_DATA=str(BASE / 'local-data'), HOME_MUSIC_ROOT=str(BASE / 'local-data' / 'audio'))
    for name in ('HOME_MUSIC_SONGLOFT_URL', 'HOME_MUSIC_SONGLOFT_USERNAME', 'HOME_MUSIC_SONGLOFT_PASSWORD'):
        os.environ.pop(name, None)
    from provider.server import main
    main()

def verify():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    key = json.loads((RUNTIME / 'music-local.json').read_text(encoding='utf-8'))['token']
    password = (RUNTIME / 'songloft-local.env').read_text(encoding='utf-8').strip().split('=', 1)[1]
    for port in (58091, 58092):
        with opener.open(f'http://127.0.0.1:{port}/', timeout=10) as response:
            assert response.status == 200
    from connect_songloft import SongloftAPI
    api = SongloftAPI({'LAN_IP':'127.0.0.1','SONGLOFT_PORT':'58091'})
    assert api.request('GET', '/api/v1/version')['version'].removeprefix('v') == '2.13.8'
    api.login('admin', password)
    songs = api.request('GET', '/api/v1/songs?type=local&limit=100&offset=0&exclude_playlist_labels=none')
    req = urllib.request.Request('http://127.0.0.1:58092/api/library', headers={'Authorization':'Bearer '+key})
    with opener.open(req, timeout=10) as response:
        tracks = json.load(response)['data']
    for track in tracks:
        if track['status'] != 'ready':
            continue
        source = BASE / 'local-data' / 'audio' / '开放音乐' / track['filename']
        with opener.open(track['play_url'], timeout=10) as response:
            assert hashlib.sha256(response.read()).digest() == hashlib.sha256(source.read_bytes()).digest()
    result = {'project_path':str(BASE),'provider_url':'http://127.0.0.1:58092',
              'songloft_url':'http://127.0.0.1:58091','provider_tracks':len(tracks),
              'songloft_tracks':songs.get('total',0),'existing_credentials_login':'passed',
              'audio_hashes':'passed','xiaomi_and_speaker':'not verified'}
    (RUNTIME / 'verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    {'init':initialize,'serve':serve,'verify':verify}[sys.argv[1]]()
