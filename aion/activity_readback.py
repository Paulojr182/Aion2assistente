"""Fixed read-only activity diagnostics for the local reader channel."""
from dataclasses import asdict
import json
from pathlib import Path
from .metadata_templates import InstructionTemplate
from .code_anchors import recover_code_anchors
from .native_inventory import observe_inventory_with_bag
from .native_interactions import observe_interactions, recover_interaction_code
from .native_contextual import observe_contextual, recover_contextual_code
from .native_bag import observe_bag_state, recover_bag_code
from .native_quests import observe_quests, recover_quest_code
from .native_gather_sources import observe_gather_sources, recover_gather_source_profile
from .native_gather_progress import observe_gather_progress, recover_gather_progress_code
from .target_code import recover_visibility
from .native_flight import discover as discover_flight_functions
from .action_bindings import observe_action_bindings,FLIGHT_ACTION_NAMES,AXIS_DIAGNOSTIC_NAMES

ACTIVITY_READ_TASKS = frozenset(('inventory', 'bag_state', 'interactions', 'contextual', 'quests', 'gather_sources', 'gather_progress','flight_functions','flight_bindings','movement_axis_bindings'))


class ActivityReadback:
    def __init__(self, base):
        self.base = Path(base)
        self.session = None
        self.inventory_anchors = None
        self.visibility = None
        self.quest_anchors = None
        self.gather_progress_anchors = None
        self.gather_source_profile = None
        self.interaction_anchors = None
        self.contextual_anchors = None
        self.bag_anchors = None

    def load(self, name):return json.loads((self.base/name).read_text('utf-8'))

    @staticmethod
    def bounded(report, field, limit):
        entries = report.get(field, [])
        return {**report, field: entries[:limit], field+'_total': len(entries),
                field+'_truncated': len(entries) > limit}

    def observe(self, process, roots, task):
        if task not in ACTIVITY_READ_TASKS:
            raise ValueError('Tarefa fora do diagnóstico de atividades.')
        if not roots:
            raise ValueError('Raízes de leitura não validadas.')
        if self.session != process.session:
            self.session = process.session
            self.inventory_anchors = None
            self.visibility = None
            self.quest_anchors = None
            self.gather_progress_anchors = None
            self.gather_source_profile = None
            self.interaction_anchors = None
            self.contextual_anchors = None
            self.bag_anchors = None
        if task == 'inventory':
            if self.inventory_anchors is None:
                templates = tuple(InstructionTemplate(d['name'],d['old_rva'],bytes.fromhex(d['expected']),
                    tuple(tuple(p) for p in d['operands']),d['radius'])
                    for d in self.load('inventory_code_templates.json'))
                self.inventory_anchors = recover_code_anchors(
                    lambda rva,size:process.read(process.base+rva,size),process.image_size,templates)
            if self.bag_anchors is None:self.bag_anchors=recover_bag_code(process,self.base)
            return self.bounded(observe_inventory_with_bag(process,roots,self.base,self.inventory_anchors,self.bag_anchors), 'items', 128)
        if task == 'bag_state':
            if self.bag_anchors is None:self.bag_anchors=recover_bag_code(process,self.base)
            return observe_bag_state(process,roots,self.base,self.bag_anchors)
        if task == 'interactions':
            if self.interaction_anchors is None:self.interaction_anchors=recover_interaction_code(process,self.base)
            return observe_interactions(process,roots,self.base,self.interaction_anchors)
        if task == 'contextual':
            if self.contextual_anchors is None:self.contextual_anchors=recover_contextual_code(process,self.base)
            definitions=self.load('target_templates.json')
            if self.visibility is None:self.visibility=recover_visibility(process,definitions)
            return observe_contextual(process,roots,self.base,self.contextual_anchors,self.visibility,definitions)
        if task == 'quests':
            if self.quest_anchors is None:self.quest_anchors=recover_quest_code(process,self.base)
            return self.bounded(observe_quests(process,roots,self.base,self.quest_anchors), 'records', 128)
        if task == 'gather_sources':
            if self.gather_source_profile is None:
                self.gather_source_profile = recover_gather_source_profile(process,self.base)
            return self.bounded(observe_gather_sources(process,roots,self.base,self.gather_source_profile), 'targets', 32)
        if task == 'flight_functions':
            report=discover_flight_functions(process,roots,include_movement=True)
            functions=[]
            for row in report['functions'][:8]:
                analysis=row['analysis']
                functions.append({**row,'analysis':{
                    **{key:value for key,value in analysis.items() if key not in (
                        'entry_instructions','direct_calls','movement_mode_write_candidates')},
                    'direct_calls':analysis['direct_calls'][:16],
                    'direct_calls_truncated':len(analysis['direct_calls'])>16,
                    'movement_mode_write_candidates':analysis['movement_mode_write_candidates'][:8],
                    'write_candidates_truncated':len(analysis['movement_mode_write_candidates'])>8}})
            return {**report,'functions':functions,'functions_total':len(report['functions']),
                    'functions_truncated':len(report['functions'])>8}
        if task in ('flight_bindings','movement_axis_bindings'):
            definitions=self.load('internal_action_templates.json')
            reports={}
            names=AXIS_DIAGNOSTIC_NAMES if task=='movement_axis_bindings' else FLIGHT_ACTION_NAMES
            options={'diagnostic_only':True} if task=='movement_axis_bindings' else {}
            for name in names:
                try:
                    report=observe_action_bindings(process,roots,definitions,action_name=name,**options)
                    reports[name]=self.bounded(report,'bindings',8)
                except (ValueError,OSError) as exc:
                    reports[name]={'ok':False,'error_type':type(exc).__name__,'error':str(exc)}
            return {'session':process.session,'actions':reports,'game_actions_executed':False,
                    'flight_dispatch_approved':False,'axis_dispatch_approved':False}
        definitions = self.load('target_templates.json')
        if self.visibility is None:self.visibility = recover_visibility(process,definitions)
        if self.gather_progress_anchors is None:
            self.gather_progress_anchors = recover_gather_progress_code(process,self.base)
        frame = observe_gather_progress(process,roots,self.base,self.visibility,definitions,self.gather_progress_anchors)
        return {**asdict(frame), 'game_actions_executed': False}
