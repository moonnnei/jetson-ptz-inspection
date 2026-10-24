"""Import finite series history in one registry transaction, without pose promotion."""
import hashlib
from pathlib import Path

from capture_round import validate_round
from capture_round_history import prepare_round_job
from capture_series import read_records,summarize,validate_series
from store import Store


def import_series(store,directory,revision):
    root=Path(directory).resolve();series,records=read_records(root)
    if not records:raise ValueError('series has no recorded round to import')
    state=store.snapshot();validate_series(series['plan'],state)
    if revision!=state['revision']:raise ValueError('series import needs current original review revision')
    key='series-'+hashlib.sha256((root/'series.json').read_bytes()).hexdigest()[:16]
    jobs=[]
    for index,item in enumerate(series['rounds'],1):
        job,plan=prepare_round_job(store,root/item['directory'],revision,state)
        job.update(series_id=key,series_round_index=index,series_planned_rounds=series['plan']['rounds'])
        jobs.append(job)
    summary=summarize(series,records)
    def apply(current):
        validate_series(series['plan'],dict(current,revision=revision))
        Store.editable(current,series['plan']['round_plan']['map_id'])
        if any(j['id'] in {x['id'] for x in jobs} or j.get('series_id')==key for j in current['jobs']):
            raise ValueError('series or one of its rounds already imported')
        for job in jobs:current['jobs'].append(job)
        current['jobs'][-1]['series_summary']=summary
        Store.pause(current,'finite_repeatability_trial_imported_periodic_not_enabled')
        return dict(series_id=key,round_ids=[j['id'] for j in jobs],summary=summary)
    return store.change(revision,'capture_series_import',dict(id=key,rounds=len(jobs)),apply)
