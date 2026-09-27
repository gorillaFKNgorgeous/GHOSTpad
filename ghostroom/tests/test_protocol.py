# SPDX-License-Identifier: GPL-2.0-or-later
"""Validate GHOSTroom protocol fixtures against the JSON Schemas.

fixtures/valid/<schema>/*.json must validate.
fixtures/invalid/<schema>/*.json are patches {why, base, set?, remove?} applied
to a valid fixture, so each one fails for exactly the reason it states.
"""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

PROTOCOL = Path(__file__).resolve().parents[1] / 'protocol'
SCHEMAS = PROTOCOL / 'schemas'
FIXTURES = PROTOCOL / 'fixtures'
BASE_ID = 'https://schemas.ghostpad.invalid/ghostroom/v0/'


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _registry():
    resources = []
    for path in sorted(SCHEMAS.rglob('*.schema.json')):
        schema = _load(path)
        expected = BASE_ID + path.relative_to(SCHEMAS).as_posix()
        assert schema['$id'] == expected, f'{path} $id must be {expected}'
        resources.append((schema['$id'], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


REGISTRY = _registry()


def _validator(schema_key):
    return Draft202012Validator({'$ref': BASE_ID + schema_key + '.schema.json'}, registry=REGISTRY)


def _schema_key(fixture, kind):
    parts = fixture.relative_to(FIXTURES / kind).parts
    return '/'.join(parts[:2]) if parts[0] == 'legacy' else parts[0]


def _fixtures(kind):
    return sorted((FIXTURES / kind).rglob('*.json'))


def _pointer(path):
    return [part.replace('~1', '/').replace('~0', '~') for part in path.lstrip('/').split('/')]


def _parent(doc, parts):
    for part in parts[:-1]:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc


def _key(container, part):
    return int(part) if isinstance(container, list) else part


def _build_invalid(spec):
    base = FIXTURES / 'valid' / spec['base']
    instance = copy.deepcopy(_load(base))
    for path in spec.get('remove', []):
        parts = _pointer(path)
        container = _parent(instance, parts)
        key = _key(container, parts[-1])
        assert key in container if isinstance(container, dict) else key < len(container), \
            f'remove path {path} does not exist in {spec["base"]}'
        del container[key]
    for path, value in spec.get('set', {}).items():
        parts = _pointer(path)
        container = _parent(instance, parts)
        container[_key(container, parts[-1])] = value
    return instance


def _ids(paths, kind):
    return [p.relative_to(FIXTURES / kind).as_posix() for p in paths]


def test_every_schema_is_valid_2020_12():
    for path in SCHEMAS.rglob('*.schema.json'):
        Draft202012Validator.check_schema(_load(path))


VALID = _fixtures('valid')
INVALID = _fixtures('invalid')


@pytest.mark.parametrize('fixture', VALID, ids=_ids(VALID, 'valid'))
def test_valid_fixture(fixture):
    errors = sorted(_validator(_schema_key(fixture, 'valid')).iter_errors(_load(fixture)), key=str)
    assert not errors, '\n'.join(f'{list(e.absolute_path)}: {e.message}' for e in errors[:5])


@pytest.mark.parametrize('fixture', INVALID, ids=_ids(INVALID, 'invalid'))
def test_invalid_fixture(fixture):
    spec = _load(fixture)
    assert spec.get('why'), 'invalid fixtures must state why they are invalid'
    key = _schema_key(fixture, 'invalid')
    assert _schema_key(FIXTURES / 'valid' / spec['base'], 'valid') == key, \
        'base fixture must belong to the same schema'
    assert spec.get('expect'), 'invalid fixtures must name the error they expect'
    validator = _validator(key)
    assert validator.is_valid(_load(FIXTURES / 'valid' / spec['base'])), 'base fixture must be valid'
    errors = _leaf_errors(validator.iter_errors(_build_invalid(spec)))
    assert errors, f'expected rejection: {spec["why"]}'
    assert any(spec['expect'] in error for error in errors), \
        f'rejected for the wrong reason; expected {spec["expect"]!r} in:\n' + '\n'.join(errors)


def _leaf_errors(errors):
    """Flatten jsonschema errors into 'path: message' lines for their deepest causes."""
    out = []
    for error in errors:
        if error.context:
            out.extend(_leaf_errors(error.context))
        else:
            out.append('/'.join(map(str, error.absolute_path)) + ': ' + error.message)
    return out


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _collect(field, schema_dir):
    found = set()
    for path in (FIXTURES / 'valid' / schema_dir).rglob('*.json'):
        for node in _walk(_load(path)):
            if isinstance(node, dict) and isinstance(node.get(field), str):
                found.add(node[field])
    return found


def _enum(schema_file, *path):
    node = _load(SCHEMAS / schema_file)
    for part in path:
        node = node[part]
    return set(node['enum'])


def test_every_schema_has_valid_and_invalid_fixtures():
    keys = {p.relative_to(SCHEMAS).as_posix()[:-len('.schema.json')]
            for p in SCHEMAS.rglob('*.schema.json')} - {'common'}
    assert keys == {_schema_key(p, 'valid') for p in VALID}
    assert keys == {_schema_key(p, 'invalid') for p in INVALID}


def test_every_event_kind_has_a_valid_example():
    kinds = _enum('event.schema.json', 'properties', 'kind')
    assert kinds == {'discuss', 'inspect', 'plan', 'execute', 'wait', 'review', 'request_input'}
    assert _collect('kind', 'event') >= kinds


def test_every_artifact_type_has_a_valid_example():
    types = _enum('artifact.schema.json', 'properties', 'type')
    assert types == {'screenshot', 'render', 'object_ref', 'checkpoint', 'task', 'review_card', 'lesson_step'}
    assert {_load(p)['type'] for p in (FIXTURES / 'valid' / 'artifact').glob('*.json')} == types


def test_required_failure_codes_exist_and_are_exercised():
    required = {'quota_exhausted', 'provider_unavailable', 'auth_expired', 'relay_unavailable',
                'blender_suspended', 'tool_timeout', 'scene_changed',
                'interrupted_after_possible_mutation'}
    assert required <= _enum('failure.schema.json', 'properties', 'code')
    used = set()
    for schema_dir in ('failure', 'event', 'agent', 'ledger'):
        used |= _collect('code', schema_dir)
    assert required <= used


def test_fixtures_carry_no_urls():
    # Capability URLs are secrets. Only the invalid fixture that proves they
    # are rejected may contain one, and it uses the reserved .invalid TLD.
    for path in FIXTURES.rglob('*.json'):
        text = path.read_text(encoding='utf-8')
        if 'https://' in text or 'http://' in text:
            assert path.name == 'screenshot_capability_url.json', path
            assert '.invalid/' in text
