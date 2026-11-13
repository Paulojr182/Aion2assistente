"""Elevated local worker for auto-macro only. Never accepts addresses or code."""
import argparse,json,os,re,time
from pathlib import Path
from aion.local_pipe import PipeAPI
from aion.combat_protocol import parse_request
from aion.macro_reader import MacroReader
from aion.macro_transport import MacroTransport
from aion.macro_service import MacroService


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid',type=int,required=True)
    parser.add_argument('--window',type=int,required=True)
    parser.add_argument('--token',required=True)
    args=parser.parse_args()
    if args.pid<=0 or args.window<=0 or not re.fullmatch('[0-9a-f]{32}',args.token):
        parser.error('Identificação do jogo/canal inválida.')
    base=Path(__file__).resolve().parent
    def factory(progress):
        reader=MacroReader(base,args.pid,args.window,progress)
        progress('Conferindo os comandos disponíveis…')
        transport=MacroTransport(reader);transport.validate(use_hook=False)
        return transport
    serve(base,args.pid,args.window,args.token,factory)


def serve(base,pid,window,token,factory):
    base=Path(base)
    service=MacroService(factory)
    api=PipeAPI(kind='combat');name='AionRebuildCombat-'+token
    channel=api.server(name)
    data_dir=base/'data';data_dir.mkdir(exist_ok=True)
    state={'token':token,'pipe_name':name,'server_pid':os.getpid(),'game_pid':pid,'created_at':time.time()}
    temporary=data_dir/('combat_session.'+token+'.tmp')
    temporary.write_text(json.dumps(state),encoding='utf-8');os.replace(temporary,data_dir/'combat_session.json')
    last_client=time.monotonic();stopping=False;claimed=False
    service.launch()
    try:
        while not stopping:
            age=time.monotonic()-last_client
            if age>3 and service.desired.is_set():service.stop()
            if age>(12 if claimed else 90):break
            if not api.accept(channel,timeout=0.25):continue
            request=None
            try:
                request=parse_request(api.receive(channel,512,timeout=1))
                last_client=time.monotonic();claimed=True
                task=request['task']
                if task=='start':service.start('combat')
                elif task=='start_gather':service.start('gathering')
                elif task=='start_quests':service.start('quests')
                elif task=='stop':service.stop()
                elif task=='scan_targets':
                    response={'id':request['id'],'ok':True,'data':service.scan_targets()}
                elif task=='shutdown':
                    service.stop()
                    if not service.shutdown():raise RuntimeError('O encerramento ainda não foi confirmado.')
                    stopping=True
                if task!='scan_targets':
                    response={'id':request['id'],'ok':True,'data':service.get_status()}
            except (OSError,ValueError,RuntimeError) as exc:
                response={'id':request['id'] if request else None,'ok':False,'error':str(exc)}
            try:
                api.send(channel,json.dumps(response,ensure_ascii=True).encode('utf-8'))
                if api.receive(channel,16,timeout=1)!=b'ACK':raise ValueError('Confirmação inválida.')
            except (OSError,ValueError):pass
            finally:api.k.DisconnectNamedPipe(channel)
    finally:
        before_shutdown=service.get_status()
        service.shutdown();api.k.CloseHandle(channel)
        (data_dir/'combat_last_status.json').write_text(json.dumps({'token':token,
            'before_shutdown':before_shutdown,'after_shutdown':service.get_status()},indent=2),encoding='utf-8')


if __name__=='__main__':main()
