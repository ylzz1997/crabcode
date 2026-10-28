"""Full-text templates must survive storage, activation and rendering unchanged."""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from crabcode_core.events import CoreSession
from crabcode_core.prompts.library import resolve_prompt_profile
from crabcode_core.prompts.presets import builtin_templates, get_builtin_template
from crabcode_core.prompts.system import get_system_prompt
from crabcode_core.types.config import CrabCodeSettings
from crabcode_gateway.routes import config as routes


def test_presets_resolve_without_merging_default_behavior_or_config_overrides():
    templates = builtin_templates()
    assert [t.name for t in templates] == ['Codex', 'Claude Code', 'DSH', 'ZCode', 'OpenCode', 'Cursor', 'Qoder', 'Kimi Code']
    for template in templates:
        text = template.sections['native_system']
        assert len(text) > 1000
        assert '${' not in text
        assert 'multi_tool_use.parallel' not in text
        settings = CrabCodeSettings(active_prompt_template=template.id, prompt_templates=[
            {'id': template.id, 'name': 'Spoof', 'sections': {'native_system': 'spoof'}}
        ])
        profile = resolve_prompt_profile(settings)
        assert profile.native_system == text
        prompt = get_system_prompt(['Skill'], 'actual-model', cwd='/actual/cwd', profile=profile, language='Chinese')
        assert prompt[0] == text
        assert '# Git safety' not in prompt
        assert '# Tools' not in prompt
        assert any('actual-model' in part and '/actual/cwd' in part for part in prompt)
        assert any('Always respond in Chinese' in part for part in prompt)
        plan = get_system_prompt(['Skill'], 'actual-model', profile=profile, agent_mode='plan')
        assert any('MUST NOT make any edits' in part for part in plan)
        assert plan.static_count < len(plan)
    templates[0].sections['native_system'] = 'mutated'
    assert get_builtin_template('builtin:codex').sections['native_system'] != 'mutated'


def test_native_template_api_roundtrip_and_live_activation(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    project = tmp_path / 'project'; project.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(routes, '_resolve_model_settings_cwd', lambda request, cwd: str(project))
    session = CoreSession(cwd=str(project))
    app = FastAPI(); app.include_router(routes.router)
    app.state.sessions = {session.session_id: session}
    with TestClient(app) as client:
        def mutate(**kwargs):
            return client.post('/config/prompt-settings', json={'source': 'userSettings', 'cwd': str(project), **kwargs})
        for t in builtin_templates():
            result = mutate(action='set_active_template', template_id=t.id)
            assert result.status_code == 200
            assert session._prompt_profile.native_system == t.sections['native_system']
        stored_path = home / '.crabcode/settings.json'
        assert 'prompt_templates' not in json.loads(stored_path.read_text())
        for action in ('save_template', 'delete_template'):
            result = mutate(action=action, template_id=' builtin:codex ', template_name='Changed')
            assert result.status_code == 400
        text = '\n  # Retain whitespace\n' + ('A sentence. ' * 2500) + '\n\n'
        saved = mutate(action='save_template', template_name='Codex 副本', sections={'native_system': text, 'extra': ' extra '})
        assert saved.status_code == 200
        assert session._prompt_profile.native_system == text
        custom = next(t for t in saved.json()['templates'] if not t['builtin'])
        assert custom['sections'] == {'native_system': text, 'extra': 'extra'}
        assert json.loads(stored_path.read_text())['prompt_templates'][0]['sections']['native_system'] == text
        assert mutate(action='save_template', template_name='Too long', sections={'native_system': 'a' * 128001}).status_code == 422
        assert mutate(action='set_active_template', template_id=None).status_code == 200
        assert session._prompt_profile is None
