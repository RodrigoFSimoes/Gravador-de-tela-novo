# Gravador Compacto

Grava a tela com áudio do microfone e já salva o MP4 comprimido para caber no limite (padrão 5 MB).

## Gerar o .exe
1. Suba estes arquivos para um repositório no GitHub (branch `main`).
2. Aba **Actions** > **Build EXE** > aguarde terminar.
3. Baixe o artefato **GravadorCompacto** (zip). Ele traz `GravadorCompacto.exe` + `ffmpeg.exe`. Mantenha os dois na mesma pasta.

## Rodar sem compilar
Precisa de Python 3 e `ffmpeg.exe` na PATH ou ao lado do `main.py`:
    python main.py

## Uso
Escolha o microfone, selecione a área (ou tela cheia), defina o limite em MB, clique em Gravar e depois em Parar e salvar.
