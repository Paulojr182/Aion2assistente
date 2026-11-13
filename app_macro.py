"""Focused desktop panel: native combo macro and automatic next enemy."""
import json,queue,subprocess,threading,time,uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from aion.taskbar_status import set_overlay
from aion.attack_hotkey import AttackHotkey, parse_hotkey, capture_key
from aion.combat_protocol import request

BASE=Path(__file__).resolve().parent


def bot_env():
    import os,sys
    python=Path(sys.executable)
    if python.name.lower()=='pythonw.exe':python=python.with_name('python.exe')
    return {**os.environ,'AION_BOT_PYTHON':str(python)}
BG='#0b1119';PANEL='#121e2b';TEXT='#e4edf7';MUTED='#a7bdd3';GOLD='#e8be71'


class App:
    def __init__(self,root):
        self.root=root;self.events=queue.Queue();self.pipe=None
        self.busy=False;self.want_running=False;self.pending_stop=False;self.pending_start=False;self.closing=False
        self.current_page='combat';self.desired_task='start'
        self.cancel_connect=threading.Event();self.last_status={}
        self.active_icon=False
        root.title('AION 2 | Ataque automático');root.geometry('1060x720');root.minsize(900,600);root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW',self.close)
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('.',background=PANEL,foreground=TEXT,fieldbackground='#1b2c3f',font=('Segoe UI',10))
        style.configure('TButton',padding=(16,10),borderwidth=0)
        style.map('TButton',background=[('active','#283c51')],foreground=[('disabled','#697b8e')])
        style.configure('Accent.TButton',background=GOLD,foreground=BG,font=('Segoe UI',10,'bold'))
        style.map('TCombobox',fieldbackground=[('readonly','#1b2c3f')],foreground=[('readonly',TEXT)],selectbackground=[('readonly','#1b2c3f')],selectforeground=[('readonly',TEXT)])
        rail=tk.Frame(root,bg='#101923',width=205);rail.pack(side='left',fill='y');rail.pack_propagate(False)
        tk.Label(rail,text='AION 2',bg='#101923',fg=GOLD,font=('Segoe UI',26,'bold')).pack(anchor='w',padx=22,pady=(28,2))
        tk.Label(rail,text='Assistente de combate',bg='#101923',fg=MUTED,font=('Segoe UI',9)).pack(anchor='w',padx=22,pady=(0,30))
        ttk.Button(rail,text='Ataque automático',command=lambda:self.show('combat')).pack(fill='x',padx=12,pady=3)
        ttk.Button(rail,text='Coleta',command=lambda:self.show('gathering')).pack(fill='x',padx=12,pady=3)
        ttk.Button(rail,text='Como funciona',command=lambda:self.show('help')).pack(fill='x',padx=12,pady=3)
        self.sync_button=ttk.Button(rail,text='Sincronizar versão do jogo',command=self.sync_version)
        self.sync_button.pack(fill='x',padx=12,pady=(18,3))
        tk.Label(rail,text='Use depois que o AION 2\natualizar (jogo aberto).',justify='left',bg='#101923',fg=MUTED,font=('Segoe UI',8)).pack(anchor='w',padx=22)
        body=tk.Frame(root,bg=BG);body.pack(fill='both',expand=True,padx=27,pady=23)
        self.badge=tk.Label(body,text='Pronto para conectar',bg='#302b22',fg=GOLD,padx=12,pady=8,font=('Segoe UI',10,'bold'))
        self.badge.pack(anchor='w',pady=(0,22))
        self.heading=tk.Label(body,text='Ataque automático',bg=BG,fg=TEXT,font=('Segoe UI',25,'bold'));self.heading.pack(anchor='w')
        self.subtitle=tk.Label(body,text='O macro executa o combo. Ao eliminar o alvo, o assistente procura outro inimigo.',
            bg=BG,fg=MUTED,justify='left',wraplength=690,font=('Segoe UI',10));self.subtitle.pack(anchor='w',pady=(9,22))
        footer=tk.Frame(body,bg=BG);footer.pack(side='bottom',fill='x',pady=(12,0))
        self.content=tk.Frame(body,bg=BG);self.content.pack(fill='both',expand=True)
        self.status=tk.StringVar(value='Abra o AION 2 e entre no personagem para iniciar.')
        self.health=tk.StringVar(value='—');self.target=tk.StringVar(value='Nenhum alvo confirmado')
        self.macro=tk.StringVar(value='—');self.detail=tk.StringVar(value='')
        self.hotkey_path=BASE/'data'/'attack_hotkey.json'
        self.hotkey_value=''
        self.hotkey_dialog=None
        self.hotkey=AttackHotkey(self.events)
        self.hotkey_button=ttk.Button(footer,text='Escolher tecla',command=self.configure_hotkey)
        self.hotkey_button.pack(side='left')
        try:
            saved=json.loads(self.hotkey_path.read_text(encoding='utf-8'))
            if isinstance(saved,str) and saved:
                self.hotkey.commands.put(parse_hotkey(saved))
        except (OSError,ValueError):
            pass
        self.connect_button=ttk.Button(footer,text='Conectar',command=self.connect_only)
        self.connect_button.pack(side='left',padx=(12,0))
        self.start_button=ttk.Button(footer,text='Iniciar ataque',style='Accent.TButton',command=self.start)
        self.start_button.pack(side='right')
        self.stop_button=ttk.Button(footer,text='Parar',state='disabled',command=self.stop);self.stop_button.pack(side='right',padx=(0,10))
        tk.Label(body,textvariable=self.detail,bg=BG,fg=MUTED,anchor='w',wraplength=690,font=('Segoe UI',9)).pack(fill='x',pady=(12,0))
        self.show('combat');root.after(100,self.drain);root.after(500,self.poll)

    def configure_hotkey(self):
        if self.hotkey_dialog is not None:
            self.hotkey_dialog.focus_force()
            return
        dialog=tk.Toplevel(self.root)
        self.hotkey_dialog=dialog
        dialog.title('Tecla para ativar e desativar')
        dialog.transient(self.root)
        dialog.resizable(False,False)
        body=ttk.Frame(dialog,padding=20)
        body.pack(fill='both',expand=True)
        prompt=tk.StringVar(value='Pressione uma tecla. Ela inicia a rotina selecionada e, ao apertar de novo, para.')
        ttk.Label(body,textvariable=prompt).pack(pady=(0,12))
        if self.hotkey_value:
            ttk.Label(body,text=f'Tecla atual: {self.hotkey_value}').pack(pady=(0,12))

        def close_dialog():
            self.hotkey_dialog=None
            dialog.destroy()

        def on_key(event):
            if event.keysym=='Escape':
                close_dialog()
                return 'break'
            try:
                self.hotkey.commands.put(capture_key(event.keycode))
            except ValueError as exc:
                prompt.set(str(exc))
            else:
                close_dialog()
            return 'break'

        def remove():
            self.hotkey.commands.put(None)
            close_dialog()

        ttk.Button(body,text='Cancelar',command=close_dialog).pack(side='right')
        ttk.Button(body,text='Remover tecla',command=remove).pack(side='left')
        dialog.protocol('WM_DELETE_WINDOW',close_dialog)
        dialog.bind('<KeyPress>',lambda event:'break')
        dialog.bind('<KeyRelease>',on_key)
        dialog.grab_set()
        dialog.focus_force()

    def toggle_attack(self):
        if self.closing or self.hotkey_dialog is not None:
            return
        if self.want_running:
            self.stop()
        elif not self.busy and not self.pending_stop:
            self.start()

    def refresh_action_labels(self):
        label={'combat':'Iniciar ataque','gathering':'Iniciar coleta','quests':'Iniciar missões'}.get(self.current_page,'Iniciar')
        suffix=f' ({self.hotkey_value})' if self.hotkey_value else ''
        self.start_button.configure(text=label+suffix)
        self.stop_button.configure(text='Parar'+suffix)

    def show(self,page):
        for child in self.content.winfo_children():child.destroy()
        if page=='help':
            self.heading.configure(text='Como funciona')
            text=('1. Ataque automático: escolha "Macro do jogo" (o macro configurado no próprio AION 2) ou "Combo de skills" '
                  '(monte a sequência com as skills dos slots 1 a 8 da barra e o tempo de espera de cada uma).\n\n'
                  '2. Coleta: marque os recursos, ajuste o raio e a estamina mínima de voo. Ele vai sempre ao spot mais próximo, no chão ou no alto.\n\n'
                  '3. Nos spots altos, só no ajuste final, o jogo vem para a frente e o mouse é usado por alguns segundos.\n\n'
                  '4. Clique em Parar para encerrar. O jogo pode permanecer ao fundo enquanto você usa outros aplicativos.')
            tk.Label(self.content,text=text,bg=PANEL,fg=TEXT,justify='left',wraplength=650,padx=20,pady=20,font=('Segoe UI',11)).pack(fill='both',expand=True)
            return
        self.current_page=page
        self.desired_task={'combat':'start','gathering':'start_gather','quests':'start_quests'}.get(page,'start')
        self.refresh_action_labels()
        titles={'combat':'Ataque automático','gathering':'Coleta nativa','quests':'Missões nativas'}
        subtitles={
            'combat':'O macro executa o combo. Ao eliminar o alvo, o assistente procura outro inimigo.',
            'gathering':'Farm contínuo: vai sempre ao recurso marcado mais próximo dentro do raio, no chão ou no alto. Spots no alto exigem a estamina de voo mínima abaixo.',
            'quests':'Em desenvolvimento: leitor de registros e política local prontos; catálogo, alvos e comandos ainda pendentes.',
        }
        self.heading.configure(text=titles.get(page,'Ataque automático'))
        self.subtitle.configure(text=subtitles.get(page,subtitles['combat']))
        panel=tk.Frame(self.content,bg=PANEL);panel.pack(fill='both',expand=True)
        section={'combat':'Combate e combo','gathering':'Coleta e aproximação','quests':'Diário e objetivos'}.get(page,'Combate e combo')
        tk.Label(panel,text=section,bg=PANEL,fg=GOLD,font=('Segoe UI',12,'bold')).pack(anchor='w',padx=22,pady=(20,12))
        tk.Label(panel,textvariable=self.status,bg=PANEL,fg=TEXT,justify='left',wraplength=650,font=('Segoe UI',12)).pack(anchor='w',padx=22,pady=(0,22))
        for label,variable in (('Macro do jogo',self.macro),('Seu HP',self.health),('Alvo',self.target)):
            row=tk.Frame(panel,bg=PANEL);row.pack(fill='x',padx=22,pady=7)
            tk.Label(row,text=label,bg=PANEL,fg=MUTED,width=16,anchor='w',font=('Segoe UI',10)).pack(side='left')
            tk.Label(row,textvariable=variable,bg=PANEL,fg=TEXT,anchor='w',font=('Segoe UI',11,'bold')).pack(side='left')
        note={'combat':'Macro do jogo: usa as habilidades e a ordem do macro que você configurou no AION 2, em qualquer tecla.',
              'gathering':'O mouse fica livre. Só nos spots altos, no ajuste final, o jogo vem para a frente e o mouse é usado por alguns segundos e devolvido. Sem spot no raio: pousa (V) e espera nascer. Espaço na mochila não é conferido.',
              'quests':'A execução de missões ainda está em desenvolvimento.'}.get(page,'')
        if page in ('combat', 'gathering'):
            opts = self.load_options()
            area_frame = tk.Frame(panel, bg=PANEL)
            area_frame.pack(fill='x', padx=22, pady=(10, 10))

            # Left box: Area and Radius
            geo_box = tk.LabelFrame(area_frame, text='Área e Comportamento', bg=PANEL, fg=GOLD, font=('Segoe UI', 10, 'bold'), padx=12, pady=10)
            geo_box.pack(side='left', fill='both', expand=True, padx=(0, 10))

            saved_pos = opts.get('combat_positions', '')
            self.center_var = tk.StringVar(value=f"Centro salvo: {saved_pos}" if saved_pos else "Centro salvo: Não marcado")
            tk.Label(geo_box, textvariable=self.center_var, bg=PANEL, fg=TEXT, font=('Segoe UI', 9)).pack(anchor='w', pady=(0, 6))

            ttk.Button(geo_box, text='Marcar minha posição', command=lambda: self.mark_position(page)).pack(anchor='w', pady=(0, 8))

            radius_row = tk.Frame(geo_box, bg=PANEL)
            radius_row.pack(fill='x')
            tk.Label(radius_row, text='Raio:', bg=PANEL, fg=MUTED, font=('Segoe UI', 9)).pack(side='left')
            self.radius_var = tk.StringVar(value=str(opts.get('circle_radius_m', '50')))
            self.slider(radius_row, self.radius_var, 10, 300, 5, ' m', self.save_radius)
            tk.Label(geo_box, text='Raio medido a partir do centro salvo (ou de onde você iniciar, se não marcado).',
                     bg=PANEL, fg=MUTED, font=('Segoe UI', 8)).pack(anchor='w', pady=(2, 0))
            if page == 'gathering':
                stamina_row = tk.Frame(geo_box, bg=PANEL)
                stamina_row.pack(fill='x', pady=(10, 0))
                tk.Label(stamina_row, text='Estamina mín. de voo:', bg=PANEL, fg=MUTED, font=('Segoe UI', 9)).pack(side='left')
                self.stamina_var = tk.StringVar(value=str(opts.get('flight_min_stamina_pct', 70)))
                self.slider(stamina_row, self.stamina_var, 0, 100, 5, '%', self.save_stamina)
                tk.Label(geo_box, text='Abaixo disso ele coleta só no chão; acima, sobe para os spots altos.',
                         bg=PANEL, fg=MUTED, font=('Segoe UI', 8)).pack(anchor='w', pady=(2, 0))

            # Right box: Target selection
            target_box_title = 'Alvos de combate' if page == 'combat' else 'Recursos para coletar'
            target_box = tk.LabelFrame(area_frame, text=target_box_title, bg=PANEL, fg=GOLD, font=('Segoe UI', 10, 'bold'), padx=12, pady=10)
            target_box.pack(side='right', fill='both', expand=True)

            target_field = 'combat_targets' if page == 'combat' else 'gather_targets'
            saved_targets = [x.strip() for x in opts.get(target_field, '').split(',') if x.strip()]

            default_pool = list(saved_targets) if saved_targets else ([] if page == 'combat' else ['Coriolus', 'Od'])

            self.target_pool = list(default_pool)
            self.target_vars = {}
            self.nearby_targets_set = set()

            list_container = tk.Frame(target_box, bg=PANEL)
            list_container.pack(fill='both', expand=True)
            self.target_canvas = tk.Canvas(list_container, bg=PANEL, highlightthickness=0, height=130)
            target_scroll = ttk.Scrollbar(list_container, orient='vertical', command=self.target_canvas.yview)
            self.chk_frame = tk.Frame(self.target_canvas, bg=PANEL)
            self.chk_frame_id = self.target_canvas.create_window((0, 0), window=self.chk_frame, anchor='nw')
            self.chk_frame.bind('<Configure>', lambda e: self.target_canvas.configure(scrollregion=self.target_canvas.bbox('all')))
            self.target_canvas.bind('<Configure>', lambda e: self.target_canvas.itemconfigure(self.chk_frame_id, width=e.width))
            self.target_canvas.configure(yscrollcommand=target_scroll.set)
            self.target_canvas.pack(side='left', fill='both', expand=True)
            target_scroll.pack(side='right', fill='y')

            for item in self.target_pool:
                var = tk.BooleanVar(value=(item in saved_targets or not saved_targets))
                self.target_vars[item] = var

            self.rebuild_checklist(target_field)

            btn_row = tk.Frame(target_box, bg=PANEL)
            btn_row.pack(fill='x', pady=(6, 0))
            ttk.Button(btn_row, text='Marcar / desmarcar', command=lambda f=target_field: self.toggle_all_targets(f)).pack(side='left', padx=(0, 4))
            ttk.Button(btn_row, text='Atualizar lista', command=lambda: self.refresh_target_list(page)).pack(side='left', padx=(0, 4))
            ttk.Button(btn_row, text='Limpar seleção', command=lambda f=target_field: self.clear_all_targets(f)).pack(side='left')

            self.target_status_var = tk.StringVar(value=f"{sum(1 for v in self.target_vars.values() if v.get())} tipos selecionados. Aguardando leitura atual dos alvos.")
            tk.Label(target_box, textvariable=self.target_status_var, bg=PANEL, fg=MUTED, font=('Segoe UI', 8)).pack(anchor='w', pady=(4, 0))

        if page == 'combat':
            self.build_combo_editor(panel, self.load_options())
        tk.Label(panel,text=note,
            bg=PANEL,fg=MUTED,justify='left',font=('Segoe UI',10)).pack(anchor='w',padx=22,pady=(12,14))

    # --- estilo de ataque: macro do jogo ou combo de skills ---------------------------------
    def build_combo_editor(self, panel, opts):
        box = tk.LabelFrame(panel, text='Estilo de ataque', bg=PANEL, fg=GOLD, font=('Segoe UI', 10, 'bold'), padx=12, pady=8)
        box.pack(fill='x', padx=22, pady=(0, 4))
        self.style_var = tk.StringVar(value=opts.get('combat_style', 'macro'))
        row = tk.Frame(box, bg=PANEL); row.pack(fill='x')
        for value, text in (('macro', 'Macro do jogo (padrão)'), ('combo', 'Combo de skills (eu monto)')):
            tk.Radiobutton(row, text=text, value=value, variable=self.style_var, command=self.save_combo,
                           bg=PANEL, fg=TEXT, selectcolor='#22303d', activebackground=PANEL, activeforeground=GOLD,
                           font=('Segoe UI', 9)).pack(side='left', padx=(0, 16))
        self.combo_rows = []
        self.combo_frame = tk.Frame(box, bg=PANEL); self.combo_frame.pack(fill='x', pady=(6, 0))
        btns = tk.Frame(box, bg=PANEL); btns.pack(fill='x', pady=(4, 0))
        ttk.Button(btns, text='+ Adicionar skill', command=lambda: (self.add_combo_row(1, 1.0), self.save_combo())).pack(side='left', padx=(0, 6))
        tk.Label(btns, text='Slot = posição da skill na barra (1 a 8). Espera = segundos até a próxima. A sequência se repete.',
                 bg=PANEL, fg=MUTED, font=('Segoe UI', 8)).pack(side='left')
        from aion.macro_service import parse_combo
        for slot, wait in parse_combo(opts.get('combat_combo', '')) or ((1, 1.0), (2, 1.0), (3, 1.0)):
            self.add_combo_row(slot, wait)

    def add_combo_row(self, slot, wait):
        row = tk.Frame(self.combo_frame, bg=PANEL)
        n = len(self.combo_rows); row.grid(row=n // 4, column=n % 4, sticky='w', padx=(0, 18), pady=1)
        lbl = tk.Label(row, text=f'{len(self.combo_rows) + 1}º', bg=PANEL, fg=MUTED, width=3, font=('Segoe UI', 9)); lbl.pack(side='left')
        tk.Label(row, text='Slot', bg=PANEL, fg=MUTED, font=('Segoe UI', 9)).pack(side='left')
        slot_var = tk.StringVar(value=str(slot))
        cb = ttk.Combobox(row, textvariable=slot_var, values=[str(i) for i in range(1, 9)], width=3, state='readonly')
        cb.pack(side='left', padx=(4, 10)); cb.bind('<<ComboboxSelected>>', lambda e: self.save_combo())
        tk.Label(row, text='Espera (s)', bg=PANEL, fg=MUTED, font=('Segoe UI', 9)).pack(side='left')
        wait_var = tk.StringVar(value=f'{wait:g}')
        ent = ttk.Entry(row, textvariable=wait_var, width=5); ent.pack(side='left', padx=(4, 4))
        ent.bind('<FocusOut>', lambda e: self.save_combo()); ent.bind('<Return>', lambda e: self.save_combo())
        item = {'frame': row, 'slot': slot_var, 'wait': wait_var, 'label': lbl}
        def remove():
            row.destroy(); self.combo_rows.remove(item)
            for i, it in enumerate(self.combo_rows):
                it['label'].configure(text=f'{i + 1}º'); it['frame'].grid(row=i // 4, column=i % 4)
            self.save_combo()
        tk.Button(row, text='✕', command=remove, bg=PANEL, fg=MUTED, bd=0, activebackground=PANEL, activeforeground=GOLD, cursor='hand2').pack(side='left')
        self.combo_rows.append(item)

    def save_combo(self):
        if not hasattr(self, 'style_var'): return
        parts = []
        for it in getattr(self, 'combo_rows', []):
            try:
                parts.append(f"{int(it['slot'].get())}:{max(0.1, float(it['wait'].get().replace(',', '.'))):g}")
            except ValueError:
                pass
        self.save_option('combat_combo', ','.join(parts))
        self.save_option('combat_style', self.style_var.get())

    # --- sincronizacao com a versao do jogo ------------------------------------------------
    def sync_version(self):
        if self.pipe is not None and self.last_status.get('running'):
            messagebox.showinfo('Sincronizar', 'Pare o ataque ou a coleta antes de sincronizar.'); return
        if not messagebox.askyesno('Sincronizar versão do jogo',
                'Use depois que o AION 2 atualizar.\n\nO jogo precisa estar aberto (pode estar na tela de personagem ou já logado). '
                'O Windows vai pedir permissão de administrador. Leva até 1 minuto.\n\nContinuar?'):
            return
        self.sync_button.configure(state='disabled', text='Sincronizando…')
        self.status.set('Sincronizando com a versão do jogo… aguarde.')
        def run():
            import sys
            result_path = BASE / 'data' / 'sync_result.json'
            try: result_path.unlink()
            except OSError: pass
            python = Path(sys.executable)
            if python.name.lower() == 'pythonw.exe': python = python.with_name('python.exe')
            args = f"-X utf8 -B \"{BASE / 'sincronizar_versao.py'}\""
            cmd = ['powershell.exe', '-NoProfile', '-Command',
                   f"Start-Process -FilePath '{python}' -ArgumentList '{args}' -WorkingDirectory '{BASE}' -Verb RunAs -WindowStyle Hidden -Wait"]
            try:
                subprocess.run(cmd, cwd=BASE, creationflags=subprocess.CREATE_NO_WINDOW, timeout=600)
                result = json.loads(result_path.read_text('utf-8'))
            except Exception as exc:
                result = {'ok': False, 'message': f'Sincronização não concluída ({exc}). Permita o acesso de administrador e tente de novo.'}
            self.root.after(0, lambda: self.sync_done(result))
        threading.Thread(target=run, daemon=True).start()

    def sync_done(self, result):
        self.sync_button.configure(state='normal', text='Sincronizar versão do jogo')
        msg = result.get('message') or ('Sincronizado.' if result.get('ok') else 'Falha na sincronização.')
        self.status.set(msg)
        (messagebox.showinfo if result.get('ok') else messagebox.showwarning)('Sincronizar versão do jogo', msg)
        if result.get('ok') and result.get('moved') and self.pipe is not None:
            self.status.set(msg + ' Clique em Conectar novamente.')

    def load_options(self):
        p = BASE / 'data' / 'profile.json'
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding='utf-8')).get('options', {})
            except Exception:
                pass
        return {}

    def save_option(self, key, value):
        p = BASE / 'data' / 'profile.json'
        data = {'schema': 1, 'options': {}}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                pass
        data.setdefault('options', {})[key] = value
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')

    def mark_position(self, page):
        pos = self.last_status.get('player_position')
        if not pos:
            self.status.set('Posição atual indisponível. Conecte ao jogo para ler sua posição.')
            return
        pos_str = f"{pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}"
        self.save_option('combat_positions', pos_str)
        if hasattr(self, 'center_var'):
            self.center_var.set(f"Centro salvo: {pos_str}")
        self.status.set(f"Posição marcada com sucesso: ({pos_str})")

    def slider(self, parent, var, low, high, step, unit, on_save):
        """Barra deslizante ligada a uma StringVar (o valor salvo continua numerico)."""
        try: start = float(str(var.get()).replace('%', '').strip())
        except ValueError: start = low
        start = max(low, min(high, round(start / step) * step))
        shown = tk.StringVar(value=f'{start:g}{unit}')
        var.set(f'{start:g}')
        def moved(value):
            v = max(low, min(high, round(float(value) / step) * step))
            var.set(f'{v:g}'); shown.set(f'{v:g}{unit}')
        scale = tk.Scale(parent, from_=low, to=high, resolution=step, orient='horizontal', showvalue=False,
                         length=220, command=moved, bg=PANEL, fg=TEXT, troughcolor='#1b2c3f', activebackground=GOLD,
                         highlightthickness=0, bd=0, sliderlength=18)
        scale.set(start); scale.pack(side='left', padx=8)
        scale.bind('<ButtonRelease-1>', lambda e: on_save())
        scale.bind('<KeyRelease>', lambda e: on_save())
        tk.Label(parent, textvariable=shown, bg=PANEL, fg=GOLD, width=7, anchor='w', font=('Segoe UI', 10, 'bold')).pack(side='left')
        return scale

    def save_stamina(self):
        if hasattr(self, 'stamina_var'):
            try:
                v = max(0.0, min(100.0, float(self.stamina_var.get().strip().replace('%', ''))))
                self.save_option('flight_min_stamina_pct', v); self.stamina_var.set(f'{v:g}')
            except ValueError:
                pass

    def save_radius(self):
        if hasattr(self, 'radius_var'):
            val = self.radius_var.get().strip()
            try:
                r = float(val)
                self.save_option('circle_radius_m', r)
            except ValueError:
                pass

    def save_target_selection(self, field):
        if hasattr(self, 'target_vars'):
            selected = [name for name, var in self.target_vars.items() if var.get()]
            self.save_option(field, ', '.join(selected))

    def toggle_all_targets(self, field):
        if hasattr(self, 'target_vars'):
            all_checked = all(var.get() for var in self.target_vars.values())
            for var in self.target_vars.values():
                var.set(not all_checked)
            self.save_target_selection(field)
            self.update_target_status(field)

    def clear_all_targets(self, field):
        if hasattr(self, 'target_vars'):
            for var in self.target_vars.values():
                var.set(False)
            self.save_target_selection(field)
            self.update_target_status(field)

    def on_target_toggle(self, field):
        self.save_target_selection(field)
        self.update_target_status(field)

    def update_target_status(self, field):
        if hasattr(self, 'target_status_var') and hasattr(self, 'target_vars'):
            selected_count = sum(1 for v in self.target_vars.values() if v.get())
            near_count = len(self.nearby_targets_set) if hasattr(self, 'nearby_targets_set') else 0
            radius = self.radius_var.get() if hasattr(self, 'radius_var') else '50'
            self.target_status_var.set(
                f"{selected_count} selecionados. {near_count} encontrados nas proximidades (raio {radius}m)."
            )

    def refresh_target_list(self, page):
        self.save_radius()
        if not self.pipe or self.last_status.get('state') == 'error':
            if hasattr(self, 'target_status_var'):
                self.target_status_var.set('Conecte ao jogo para escanear os alvos da região.')
            return
        if hasattr(self, 'target_status_var'):
            self.target_status_var.set('Escaneando região com base no raio configurado...')
        self.job(lambda: ('scan_result', (request(self.pipe, 'scan_targets'), page)))

    def apply_scan_result(self, result_tuple):
        data, page = result_tuple
        if page != self.current_page or not hasattr(self, 'chk_frame'):
            return
        field = 'combat_targets' if page == 'combat' else 'gather_targets'
        new_items = data.get('monsters' if page == 'combat' else 'gatherables', [])
        self.nearby_targets_set = set(new_items)
        if new_items:
            old_vars = getattr(self, 'target_vars', {})
            self.target_pool = list(new_items)
            self.target_vars = {}
            for item in new_items:
                val = old_vars[item].get() if item in old_vars else True
                self.target_vars[item] = tk.BooleanVar(value=val)
        self.rebuild_checklist(field)
        selected_count = sum(1 for v in self.target_vars.values() if v.get())
        radius = self.radius_var.get() if hasattr(self, 'radius_var') else '50'
        if hasattr(self, 'target_status_var'):
            if new_items:
                self.target_status_var.set(
                    f"{selected_count} selecionados. {len(new_items)} encontrados na região (raio {radius}m)."
                )
            else:
                self.target_status_var.set(
                    f"Nenhum alvo encontrado na região (raio {radius}m). Aumente o raio ou aproxime-se."
                )
        self.save_target_selection(field)

    def rebuild_checklist(self, field):
        if not hasattr(self, 'chk_frame'):
            return
        for child in self.chk_frame.winfo_children():
            child.destroy()
        for item in self.target_pool:
            var = self.target_vars.get(item)
            if var is None:
                var = tk.BooleanVar(value=True)
                self.target_vars[item] = var
            is_near = item in self.nearby_targets_set
            tag = "nas proximidades" if is_near else "fora das proximidades agora"
            text = f"{item} · {tag}"
            chk = ttk.Checkbutton(self.chk_frame, text=text, variable=var,
                                  command=lambda f=field: self.on_target_toggle(f))
            chk.pack(anchor='w', pady=2)

    def job(self,callback):
        if self.busy:return False
        self.busy=True
        def run():
            try:self.events.put(callback())
            except (OSError,ValueError,RuntimeError) as exc:self.events.put(('error',str(exc)))
            finally:self.events.put(('job_done',None))
        threading.Thread(target=run,daemon=True).start();return True

    def start(self):
        self.save_radius(); self.save_stamina(); self.save_combo()   # garante que o servico leia os valores atuais
        self.want_running=True;self.pending_stop=False
        self.start_button.configure(state='disabled');self.stop_button.configure(state='normal');self.detail.set('')
        if self.pipe is not None and self.last_status.get('state')!='error':
            self.pending_start=True
            return
        self.connect()

    def connect_only(self):
        if not self.busy and (self.pipe is None or self.last_status.get('state')=='error'):
            self.connect()

    def connect(self):
        self.connect_button.configure(state='disabled')
        self.pipe=None;self.last_status={};self.cancel_connect.clear();token=uuid.uuid4().hex
        self.badge.configure(text='Conectando…');self.status.set('Validando o jogo. Aguarde a confirmação do Windows, se aparecer.')
        def connect():
            data=BASE/'data';data.mkdir(exist_ok=True)
            with (data/'combat_launcher.log').open('w',encoding='utf-8') as log:
                child=subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','RemoteSigned','-File',str(BASE/'IniciarCombate.ps1'),'-Token',token],
                    cwd=BASE,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW,env=bot_env())
                deadline=time.monotonic()+60
                while time.monotonic()<deadline and not self.cancel_connect.is_set():
                    try:
                        state=json.loads((data/'combat_session.json').read_text('utf-8'))
                        if state.get('token')==token:
                            status=request(state['pipe_name'],'status')
                            return ('connected',(state['pipe_name'],status))
                    except (OSError,ValueError,RuntimeError):pass
                    if child.poll() not in (None,0):
                        raise RuntimeError('Abra o AION 2, entre no personagem e permita a conexão como administrador.')
                    time.sleep(0.2)
            if self.cancel_connect.is_set():return ('cancelled',None)
            raise RuntimeError('A conexão não foi concluída. Confirme a permissão do Windows e tente novamente.')
        self.job(connect)

    def stop(self):
        self.want_running=False;self.pending_start=False
        if self.pipe is None:
            self.cancel_connect.set();self.status.set('Conexão cancelada. Nenhum ataque foi iniciado.');return
        self.pending_stop=True;self.status.set('Encerrando a rotina…')

    def update_icon(self, active):
        if active==self.active_icon:
            return
        name='AtaqueAutomaticoAtivo.ico' if active else 'AtaqueAutomatico.ico'
        icon=BASE/name
        if icon.is_file():
            self.root.iconbitmap(str(icon))
            self.active_icon=active
            try:
                set_overlay(self.root.winfo_id(),icon if active else None)
            except OSError:
                pass

    def apply_status(self,value):
        self.last_status=value
        if (value.get('requested_running') is False and value.get('state')=='stopped'
                and not self.pending_start and not self.busy):
            self.want_running=False
        self.status.set(value.get('message',''))
        self.badge.configure(text='Conectado ao jogo' if value.get('connected') else ('Validando conexão…' if value.get('state')=='initializing' else 'Desconectado'))
        self.macro.set('Ativo' if value.get('macro_active') is True else ('Desligado' if value.get('macro_active') is False else '—'))
        if 'player_health' in value:self.health.set(f"{value['player_health']:,} / {value['player_maximum']:,}".replace(',','.'))
        self.target.set(value.get('target_label') or 'Nenhum alvo confirmado')
        if value.get('detail'):
            try:
                (BASE/'data').mkdir(exist_ok=True)
                (BASE/'data/last_error.log').write_text(value['detail'],encoding='utf-8')
            except OSError:pass
            self.detail.set('Clique em Iniciar para reconectar.')
        else:self.detail.set('')
        running=value.get('running',False)
        self.update_icon(bool(running))
        self.connect_button.configure(state='disabled' if self.pipe and value.get('state')!='error' else 'normal')
        preparing=value.get('state')=='initializing' and self.want_running
        self.start_button.configure(state='disabled' if running or preparing or self.want_running or self.busy else 'normal')
        self.stop_button.configure(state='normal' if running or preparing or self.want_running else 'disabled')
        if value.get('state')=='error':self.want_running=False

    def drain(self):
        while True:
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='hotkey_toggle':
                self.toggle_attack()
            elif kind=='hotkey_saved':
                self.hotkey_value=value[0] if value else ''
                self.refresh_action_labels()
                try:
                    self.hotkey_path.parent.mkdir(exist_ok=True)
                    self.hotkey_path.write_text(json.dumps(self.hotkey_value),encoding='utf-8')
                except OSError:
                    messagebox.showwarning('Atalho', 'O atalho funciona, mas não pôde ser salvo.',parent=self.root)
            elif kind=='hotkey_error':
                messagebox.showwarning('Atalho indisponível',f'O Windows não liberou {value}. Escolha outra combinação.',parent=self.root)
            elif kind=='job_done':
                self.busy=False
                if self.last_status:self.apply_status(self.last_status)
            elif kind=='connected':
                self.pipe,status=value
                if self.want_running:self.pending_start=True
                self.apply_status(status)
            elif kind=='status':self.apply_status(value)
            elif kind=='scan_result':
                self.apply_scan_result(value)
            elif kind=='cancelled':
                self.start_button.configure(state='normal');self.stop_button.configure(state='disabled')
            elif kind=='error':
                self.update_icon(False)
                self.want_running=False;self.pipe=None;self.last_status={};self.badge.configure(text='Desconectado');self.status.set(value)
                self.start_button.configure(state='normal');self.stop_button.configure(state='disabled')
        if not self.closing:self.root.after(100,self.drain)

    def poll(self):
        if self.closing:return
        if self.pipe is not None and not self.busy:
            if self.pending_stop:
                self.pending_stop=False;self.job(lambda:('status',request(self.pipe,'stop')))
            elif self.pending_start and self.want_running:
                task=self.desired_task
                self.pending_start=False;self.job(lambda:('status',request(self.pipe,task)))
            else:self.job(lambda:('status',request(self.pipe,'status')))
        elif self.pipe is None and not self.busy and not self.want_running:
            self.start_button.configure(state='normal');self.stop_button.configure(state='disabled')
            self.connect_button.configure(state='normal')
        self.root.after(400,self.poll)

    def close(self):
        if self.closing:return
        self.closing=True;self.want_running=False;self.cancel_connect.set()
        self.hotkey.close()
        if self.pipe is None:self.root.destroy();return
        finished=threading.Event();name=self.pipe
        def shutdown():
            try:request(name,'shutdown')
            except (OSError,ValueError,RuntimeError):pass
            finally:finished.set()
        threading.Thread(target=shutdown,daemon=True).start();deadline=time.monotonic()+6
        def wait():
            if finished.is_set() or time.monotonic()>=deadline:self.root.destroy()
            else:self.root.after(50,wait)
        wait()


if __name__=='__main__':
    try:
        root=tk.Tk()
        icon=Path(__file__).resolve().with_name('AtaqueAutomatico.ico')
        if icon.is_file():
            root.iconbitmap(default=str(icon))
        App(root)
        root.mainloop()
    except Exception:
        import traceback,ctypes
        folder=BASE/'data';folder.mkdir(exist_ok=True)
        (folder/'last_error.log').write_text(traceback.format_exc(),encoding='utf-8')
        ctypes.windll.user32.MessageBoxW(None,'Não foi possível abrir o painel. Os detalhes foram salvos em data/last_error.log.',
                                       'AION 2 — erro ao iniciar',0x10)
