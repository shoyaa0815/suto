from dataclasses import asdict

from .context import ExecutionLimits


def validate_options(options: dict | None) -> dict:
    options = dict(options or {})
    allowed = {'memory', 'retrieval', 'subtasks', 'sandbox', 'max_tokens', 'max_tool_calls',
               'max_elapsed_seconds', 'max_changed_files'}
    if options.keys() - allowed:
        raise ValueError('unknown job options: ' + ', '.join(sorted(options.keys() - allowed)))
    for name in ('memory', 'retrieval', 'subtasks'):
        if name in options and type(options[name]) is not bool:
            raise ValueError(f'{name} must be a boolean')
    if options.get('sandbox', 'process') not in {'process', 'bwrap'}:
        raise ValueError('sandbox must be process or bwrap')
    defaults = asdict(ExecutionLimits())
    for key in allowed & defaults.keys():
        if key in options and (type(options[key]) is not int or not 1 <= options[key] <= defaults[key]):
            raise ValueError(f'{key} must be an integer between 1 and {defaults[key]}')
    return options


def job_limits(job) -> ExecutionLimits:
    return ExecutionLimits(**{key: value for key, value in job.options.items()
                              if key.startswith('max_')})
