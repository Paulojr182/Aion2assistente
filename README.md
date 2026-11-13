# AION 2 Assistente

Painel para ataque automatico, combo de habilidades, coleta e sincronizacao com a versao do jogo.

## Como executar

1. Baixe em **Code > Download ZIP** e extraia a pasta inteira.
2. Instale **Python 3.12 de 64 bits** pelo instalador oficial do Python para Windows, incluindo Tcl/Tk (Tkinter) e o Python Launcher.
3. Abra o AION 2 e entre no personagem.
4. Execute `Iniciar.cmd`, clique em **Conectar** e aceite a permissao de administrador do Windows.
5. Configure o ataque ou a coleta e clique em **Iniciar**.

O Python nao esta incluido. As dependencias adicionais de execucao ja estao nesta pasta.
As configuracoes de cada pessoa sao criadas automaticamente em `data/`.

## Depois de uma atualizacao do jogo

Com o jogo aberto, clique em **Sincronizar versao do jogo**, aceite a permissao do Windows e conecte novamente.
Se aparecer que a versao mudou demais, envie `data/sync_result.json` ao mantenedor para atualizacao.

## Conteudo

Esta distribuicao inclui apenas os arquivos de execucao, bibliotecas nativas e assinaturas necessarias para sincronizacao. Nao inclui configuracoes pessoais, testes de desenvolvimento, compilador ou historico de diagnosticos.
Os arquivos `probe_axis_pulse.py` e `probe_pathfind.py` sao dependencias importadas pela execucao; mantenha-os na pasta.

O combo de skills ainda precisa de validacao no jogo. A sincronizacao depende de assinaturas reconheciveis e pode exigir manutencao apos atualizacoes.
