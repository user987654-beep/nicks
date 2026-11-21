"""Immutable round-specific evidence indexes and verified continuation of legacy rounds."""
import json
import re
import time
from pathlib import Path

from . import evidence_delivery as delivery
from .evidence_bundle import create_bundle, manifest_hash, load_bundle
from .evidence_bundle import pack_required
from .snapshot import sha256_bytes
from .evidence_sources import source_entries, changed_sources


def bundle_row(db, job_id, round_):
    return db.one('SELECT * FROM nq_evidence_bundles WHERE job_id=? AND round=?', (job_id, round_))


def sections(text):
    pattern = r'^=== (.*?) ([0-9a-f]+) START ===\n(.*?)^=== \1 \2 END ===\n?'
    matches = list(re.finditer(pattern, text, re.M | re.S))
    has_history = (len(matches) == 10 and
                   matches[-1][1] == 'PRIOR JUDGMENT BEFORE CORRECTION, VERBATIM')
    if (len(matches) != 9 and not has_history) or len({m[1] for m in matches}) != len(matches):
        raise ValueError('cannot losslessly index this historical evidence format')
    return matches


def round_entries(db, job, rnd):
    """Index the frozen rendering plus exact archived originals, without reinterpreting findings."""
    text = Path(rnd['evidence_path']).read_bytes().decode('utf-8')
    if sha256_bytes(text.encode()) != rnd['evidence_sha256']:
        raise ValueError('frozen evidence hash mismatch')
    parts = sections(text)
    # A continuation indexes originals frozen at the first opening, never later
    # replacement investigations/reviews that happen to share this job ID.
    first = db.one('SELECT min(opened_at) cutoff FROM nq_rounds WHERE job_id=? AND evidence_sha256=?',
                   (job['id'], rnd['evidence_sha256']))
    cutoff = first['cutoff'] if first and first['cutoff'] is not None else rnd['opened_at']
    entries = [{'label': 'Frozen artifact, scope and coverage metadata', 'data': text[:parts[0].start()].encode()}]
    source_views = []
    for rel, raw in ([] if job.get('params', {}).get('rework') else changed_sources(job['snapshot'])):
        # A removed path is represented by the immutable diff. A present path must
        # be readable and is retained as its exact raw original.
        for item in source_entries(rel, raw):
            if item['label'].startswith(('UTF-8 source view:', 'Decoded source view:')):
                source_views.append(item['data'])
            else:
                entries.append(item)
    if source_views:
        entries.append({'label': 'Complete changed UTF-8 and decoded source views',
                        'data': b''.join(source_views), 'required': True})
    # Original diff bytes, not a newly generated diff or a normalized rendering.
    raw_diff = Path(job['snapshot']['diff_path']).read_bytes()
    if sha256_bytes(raw_diff) != job['snapshot']['diff_sha256']:
        raise ValueError('original reviewed diff hash mismatch')
    if raw_diff.decode().replace('\r\n','\n').rstrip() != parts[0][3].replace('\r\n','\n').rstrip():
        raise ValueError('frozen diff differs from reviewed artifact')
    entries.append({'label': 'Complete reviewed Git diff', 'data': raw_diff})
    # The second changed-source rendering differs only in framing. Prove that
    # equivalence before making its exact archived bytes requestable.
    from .nickquisition import rendered_sources
    redundant_sources = (not job.get('params', {}).get('rework') and
                         parts[1][3].rstrip() == rendered_sources(job['snapshot']).rstrip())
    entries.append({'label': parts[1][1], 'data': parts[1][3].encode(),
                    'required': not redundant_sources})
    entries.append({'label': parts[2][1], 'data': parts[2][3].encode()})
    # The rendered aggregate remains archived. Send individual raw reviews instead of
    # their second rendered copy; never strip or summarize the original response bytes.
    council = [a for a in db.attempts(job['id']) if a['status']=='ok' and (a['finished_at'] or 0) <= cutoff]
    for a in sorted(council, key=lambda a: a['slot']):
        raw = (Path(a['dir'])/'response.txt').read_bytes()
        if a['response_sha256'] and sha256_bytes(raw) != a['response_sha256']:
            raise ValueError('original Council review hash mismatch')
        if raw.decode().replace('\r\n','\n').rstrip() not in parts[3][3].replace('\r\n','\n'):
            raise ValueError('original Council review does not match frozen evidence')
        entries.append({'label': f"Original Council slot {a['slot']} {a['route']}", 'data':raw})
    if not council:
        entries.append({'label':parts[3][1], 'data':parts[3][3].encode()})
    for i in range(4,8):
        # Historical renderings are themselves frozen originals. Preserve exact raw
        # response bytes as an additional reference when the original run is available.
        entries.append({'label':parts[i][1], 'data':parts[i][3].encode()})
    for member, stage in [('good','investigation'),('evil','attack'),('evil','challenge'),('neutral','review')]:
        runs=[r for r in db.adversary_runs(job['id'],stage,member=member)
              if r['status']=='ok' and (r['finished_at'] or 0)<=cutoff]
        if runs:
            r=runs[-1];raw=(Path(r['dir'])/'response.txt').read_bytes()
            if r['response_sha256'] and sha256_bytes(raw)!=r['response_sha256']:
                raise ValueError('original judicial response hash mismatch')
            entries.append({'label':f'Exact raw {member} {stage} response', 'data':raw})
            part = parts[{('good','investigation'):4, ('evil','attack'):5,
                          ('evil','challenge'):6, ('neutral','review'):7}[(member,stage)]]
            if raw.decode('utf-8').rstrip() == part[3].rstrip():
                for entry in entries:
                    if entry['label'] == part[1]:
                        entry['required'] = False  # same original, only boundary framing differs
    entries.append({'label':parts[8][1]+' (legacy jobs retained only output tails)', 'data':parts[8][3].encode()})
    if len(parts) == 10:
        # Corrections retain prior findings as history. They must reach every
        # judge even when the common package exceeds the inline request limit.
        entries.append({'label':parts[9][1], 'data':parts[9][3].encode(), 'required':True})
    gates=json.loads(rnd['gates'] or '{}')
    for side in ('head_runs','base_runs'):
        for i, run in enumerate(gates.get(side, [])):
            if run.get('output_path'):
                raw=Path(run['output_path']).read_bytes()
                if delivery.digest(raw)!=run['output_sha256'] or len(raw)!=run['output_bytes']:
                    raise ValueError('original complete test output changed or missing')
                entries.append({'label':f'Complete verification output {side} {i+1}', 'data':raw})
    entries.append({'label':'Archived complete original evidence rendering (redundant view, requestable)',
                    'data':text.encode(), 'required':False})
    return entries


def attach(db, job, rnd):
    existing=bundle_row(db,job['id'],rnd['round'])
    if existing: return existing
    path=Path(rnd['evidence_path']).parent/'bundle-v2'
    error=None; root_hash=''
    try:
        manifest=create_bundle(path,rnd['artifact'],pack_required(round_entries(db,job,rnd)),delivery.PAGE_BYTES)
        root_hash=manifest_hash(manifest)
    except (ValueError,OSError,KeyError) as exc:
        error='INCOMPLETE evidence packaging: '+str(exc)
    row=dict(job_id=job['id'],round=rnd['round'],artifact=rnd['artifact'],manifest_path=str(path/'manifest.json'),
             manifest_sha256=root_hash,source_sha256=rnd['evidence_sha256'],delivery_version=2,error=error)
    db.execute('INSERT INTO nq_evidence_bundles ('+', '.join(row)+') VALUES ('+', '.join('?' for _ in row)+')',tuple(row.values()))
    return row


def validate_bundle(db, job, rnd):
    row=bundle_row(db,job['id'],rnd['round'])
    if not row: return None
    if row['error']: raise ValueError(row['error'])
    if row['artifact']!=rnd['artifact'] or row['source_sha256']!=rnd['evidence_sha256']:
        raise ValueError('round evidence binding changed')
    from .nickquisition import artifact_identity
    if artifact_identity(job)[0] != rnd['artifact']:
        raise ValueError('reviewed artifact changed during evidence delivery')
    return load_bundle(Path(row['manifest_path']).parent,row['manifest_sha256'],rnd['artifact'])


def scale_round(db, job_id, source_round=None):
    """Explicit continuation: keep original result/files and artifact, reuse all investigations."""
    from . import nickquisition as nq
    from .review import job_dir,job_lock
    with job_lock(job_id):
        cur=nq._round(db,job_id,None)
        if bundle_row(db,job_id,cur['round']): return cur
        src=nq._round(db,job_id,source_round)
        if nq.ballots(db,job_id,src['round']):
            raise nq.NickquisitionError('cannot migrate a partly/already voted legacy round; immutable ballots remain preserved')
        verified=nq.verify(db,job_id,src['round'])
        if verified['integrity']!='ok' or verified['verdict']!='INCOMPLETE':
            raise nq.NickquisitionError('only an intact INCOMPLETE legacy round without ballots can be scaled')
        job=db.get_job(job_id); round_=cur['round']+1
        folder=job_dir(job_id)/'nickquisition'/f'round-{round_}'
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/'evidence.md'
        data=Path(src['evidence_path']).read_bytes()
        if path.exists() and path.read_bytes()!=data: raise ValueError('continuation evidence already differs')
        path.write_bytes(data)
        row={k:src[k] for k in ['job_id','artifact','artifact_detail','evidence_sha256','gates']}
        row.update(round=round_,evidence_path=str(path),opened_at=time.time())
        db.execute('INSERT INTO nq_rounds ('+', '.join(row)+') VALUES ('+', '.join('?' for _ in row)+')',tuple(row.values()))
        attach(db,job,row)
        return nq._round(db,job_id,round_)


def deliberation_purpose(rnd, member, own_review=None):
    from . import nickquisition as nq
    purpose=member+f" nq{rnd['round']}-deliberation\n"+nq.ROLE[member]+'\nThis is the single deliberation round. Evaluate the shared original evidence independently.'
    if own_review:
        purpose+='\nYour own replacement blind review: '+(Path(own_review['dir'])/'response.txt').read_bytes().decode('utf-8')
    return purpose


def deliberation(db,job,rnd,member,runner,provider,model,out,timeout,own_review=None, *, purpose=None):
    from .evil import EvilResult
    row=bundle_row(db,job['id'],rnd['round'])
    try:
        validate_bundle(db,job,rnd)
        if row['delivery_version'] != 2:
            raise ValueError('historical delivery bundles are read-only')
        if purpose is None:
            purpose=deliberation_purpose(rnd,member,own_review)
        if member=='neutral' and provider!='anthropic':
            from .evil import CodexRunner
            runner=CodexRunner(sandbox=runner.sandbox,model=model,binary=runner.binary,
                               sandbox_extra_ro=runner.extra_ro,sandbox_extra_rw=runner.extra_rw)
        scope=delivery.digest((provider+':'+str(model)).encode())[:16]
        store=Path(rnd['evidence_path']).parent/'delivery'/member/scope
        deadline=time.monotonic()+timeout
        return delivery.run(Path(row['manifest_path']).parent,row['manifest_sha256'],rnd['artifact'],store,purpose,
                            lambda p,d: delivery.invoke_existing(runner,provider,model,p,Path(job['snapshot']['snapshot_dir']),d,max(1,deadline-time.monotonic()),bounded=True),
                            identity=lambda:getattr(runner,'provider',provider),
                            restore_identity=lambda identity:delivery.pin_runner(runner,identity),output=out,deadline=deadline,
                            scope=delivery.production_scope(job['id'],rnd['artifact'],rnd['round'],member,f"nq{rnd['round']}-deliberation",delivery.digest(purpose.encode('utf-8'))))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        out.mkdir(parents=True,exist_ok=True);(out/'response.txt').write_text('')
        return EvilResult(ok=False,error_class='evidence',error='INCOMPLETE evidence delivery: '+str(exc))


def ballot_delivery(db,job,rnd,member,delib):
    row=bundle_row(db,job['id'],rnd['round'])
    if not row:return {}
    validate_bundle(db,job,rnd)
    directory=Path(delib['dir']);ref=directory/'delivery-ref.json'
    data=json.loads(ref.read_text())
    if Path(data['bundle']).resolve()!=Path(row['manifest_path']).parent.resolve():
        raise ValueError('judge received a different evidence bundle')
    cp=Path(data['receipt']).resolve(); allowed=(Path(rnd['evidence_path']).parent/'delivery'/member).resolve()
    if not cp.is_relative_to(allowed):raise ValueError('judge receipt outside its private delivery directory')
    version=row['delivery_version']
    if version not in (1, 2): raise ValueError('unknown delivery version')
    scope=None
    if version == 2:
        stage=f"nq{rnd['round']}-deliberation"
        matches=[run for run in db.adversary_runs(job['id'],stage,member=member)
                 if run['status']=='ok' and run['model']==delib['model']
                 and Path(run['dir']).resolve()==directory.resolve()]
        if len(matches)!=1 or not re.fullmatch(r'[0-9a-f]{64}', matches[0].get('prompt_sha256') or ''):
            raise ValueError('missing attributed deliberation purpose')
        scope=delivery.production_scope(job['id'],rnd['artifact'],rnd['round'],member,stage,matches[0]['prompt_sha256'])
    hash_=delivery.verify_receipt(ref,row['manifest_sha256'],rnd['artifact'],member,
                                  (directory/'response.txt').read_bytes().decode('utf-8') if version == 2 else (directory/'response.txt').read_text(),delib['model'],
                                  scope=scope if version == 2 else None, legacy=version == 1)
    return {'delivery_sha256':hash_,'manifest_sha256':row['manifest_sha256']}
