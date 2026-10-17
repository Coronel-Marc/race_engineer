# Plano de ação — fundação do Race Engineer

## Objetivo

Construir um processo Python externo ao Assetto Corsa que leia a memória compartilhada de forma passiva, produza telemetria consistente em tempo real, mantenha estado derivado em RAM, persista eventos de volta sem bloquear a aquisição e consulte um Small Language Model local apenas sob demanda.

O caminho crítico deve ser:

```text
mmap -> snapshot imutável -> cálculo determinístico -> publicação não bloqueante
```

Nenhuma escrita em disco, chamada HTTP para o Ollama, saída de áudio ou impressão no terminal pode bloquear a aquisição da telemetria.

## Estado atual e restrições conhecidas

- [`src/main.py`](./src/main.py) executa um único laço, lê campos diretamente do `mmap`, atualiza [`LapTracker`](./src/lap_tracker.py) e imprime a cada 100 ms.
- [`src/sim_info.py`](./src/sim_info.py) expõe as páginas `physics`, `graphics` e `static` por meio de `ctypes.Structure.from_buffer`.
- [`src/lap_tracker.py`](./src/lap_tracker.py) detecta o fechamento de volta, mas, quando perde uma ou mais voltas, atribui o consumo agregado à volta mais recente.
- [`src/fuel_tracker.py`](./src/fuel_tracker.py) ainda não contém implementação.
- Os testes atuais cobrem apenas o comportamento básico do tracker em [`tests/test_lap_tracker.py`](./tests/test_lap_tracker.py).

Esses pontos devem ser tratados antes de adicionar Ollama ou áudio.

## Arquitetura-alvo

```text
                         +-------------------+
                         |  Shared memory    |
                         | physics/graphics  |
                         +---------+---------+
                                   |
                         [TelemetrySampler]
                                   |
                         TelemetrySnapshot
                                   |
                    [StateReducer / LapTracker]
                         /         |          \
                        /          |           \
             latest snapshot   LapEvent     AlertEvent
                   |              |             |
          UI/PTT/Ollama       CSV writer    Audio worker
```

### Regras de propriedade

1. Apenas `TelemetrySampler` acessa o `mmap`.
2. Apenas `StateReducer` modifica deques, acumuladores e máquinas de estado.
3. Consumidores recebem dataclasses imutáveis, sem referências a estruturas `ctypes` vivas.
4. Cada consumidor possui fila limitada e política explícita de descarte.
5. O CSV possui um único escritor.
6. Ollama, áudio, terminal e disco ficam fora do caminho crítico.

## Fase 0 — Contratos e decisões técnicas

- [ ] Definir `session_id`, `session_epoch`, `sample_sequence` e `schema_version`.
- [ ] Definir a ordem dos pneus como `(fl, fr, rl, rr)` em todo o sistema.
- [ ] Definir unidades: km/h, rpm, litros, milissegundos, graus Celsius e pressão conforme a origem.
- [ ] Definir quais valores podem ser `None`, `NaN` ou `unknown`.
- [ ] Definir política de qualidade: `OK`, `STALE`, `INCOMPLETE`, `SESSION_RESET`, `RECONNECTING`.
- [ ] Registrar quais campos são realmente fornecidos pelo Assetto Corsa e quais são derivados.
- [ ] Definir o que é “volta válida”; não inferir validade apenas comparando tempo da volta com melhor tempo.

Entregável: documento curto de contrato ou módulo `domain_models.py` com tipos e invariantes.

## Fase 1 — Captura consistente do `mmap`

Criar um adaptador dedicado, separado de [`src/sim_info.py`](./src/sim_info.py).

- [ ] Implementar `RawTelemetryReader` para abrir, ler e fechar os três mapas nomeados.
- [ ] Copiar todos os campos necessários para valores Python simples em uma única operação lógica.
- [ ] Usar `packetId` como mecanismo de detecção de atualização, após validar o comportamento do produtor do simulador.
- [ ] Considerar leitura `antes -> campos -> depois` e rejeitar/repetir a amostra se o identificador mudar durante a cópia.
- [ ] Capturar `time.monotonic_ns()` no instante da amostra.
- [ ] Tratar Assetto Corsa fechado, mapas ausentes, reconexão e sessão pausada.
- [ ] Encerrar explicitamente as estruturas `ctypes` e os `mmap`; não depender de `__del__`.

Criar uma estrutura semelhante a:

```python
@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    sequence: int
    monotonic_ns: int
    physics_packet_id: int
    graphics_packet_id: int
    session: int
    status: int
    completed_laps: int
    speed_kmh: float
    gear: int
    rpm: int
    fuel_l: float
    tyre_temperature_c: tuple[float, float, float, float]
    tyre_pressure: tuple[float, float, float, float]
    tyre_wear_pct: tuple[float, float, float, float]
```

Entregável: leitor que consegue produzir snapshots sem expor buffers compartilhados aos demais componentes.

## Fase 2 — Loop de aquisição e concorrência

- [ ] Criar `stop_event` compartilhado para shutdown limpo.
- [ ] Executar o sampler em uma thread dedicada com período configurável.
- [ ] Usar `time.monotonic_ns()` e deadline incremental; evitar depender apenas de `sleep(0.1)`.
- [ ] Publicar o snapshot mais recente em uma estrutura de capacidade 1, com política “latest wins”.
- [ ] Publicar eventos de negócio em filas limitadas, preservando a ordem.
- [ ] Nunca esperar bloqueado quando uma fila de telemetria estiver cheia.
- [ ] Medir duração da iteração e contar amostras descartadas.
- [ ] Manter o processo em prioridade normal; não competir agressivamente com o simulador.

Para esta fundação, preferir `threading` e `queue.Queue`. `asyncio` só deve ser introduzido se houver necessidade real de múltiplas integrações assíncronas; chamadas bloqueantes não podem ser executadas diretamente no event loop.

## Fase 3 — Estado derivado em RAM

Criar `StateReducer`, dono exclusivo dos dados voláteis.

- [ ] Usar `deque(maxlen=...)` para snapshots, setores, voltas e tendências.
- [ ] Armazenar timestamps e qualidade junto com cada valor.
- [ ] Calcular derivadas térmicas e de pressão usando o `dt` real, com proteção contra `dt <= 0`, amostras duplicadas e saltos de tempo.
- [ ] Aplicar suavização robusta, como EMA ou mediana curta, antes de emitir alertas.
- [ ] Manter melhor volta, melhor setor e delta por sessão.
- [ ] Resetar o estado em troca de sessão, pista, carro ou reinício do contador de voltas.
- [ ] Separar métricas brutas, métricas derivadas e estado de alerta.

### Tracker de volta

Evoluir [`LapTracker`](./src/lap_tracker.py) para retornar eventos explícitos:

- [ ] `LapCompleted` para avanço de exatamente uma volta.
- [ ] `LapGapDetected` quando o contador avançar mais de uma volta.
- [ ] `SessionReset` quando a sessão for reiniciada.
- [ ] `data_quality` em todos os eventos.
- [ ] `fuel_start`, `fuel_end` e `fuel_delta_net`, sem prometer exatidão quando houver amostras ausentes ou reabastecimento.
- [ ] Identificar reabastecimento e separar “consumo líquido” de “combustível usado”.
- [ ] Capturar setores e marcar setores ausentes como `unknown`.

O cálculo de consumo não deve usar `max(0.0, ...)` como forma de esconder combustível crescente ou dados inválidos.

### Pneus

- [ ] Acumular soma, contagem, mínimo e máximo por pneu.
- [ ] Calcular média por tempo ou distância, documentando a escolha.
- [ ] Excluir ou marcar amostras de pit lane, pausa e replay.
- [ ] Capturar temperatura, pressão e desgaste no instante da linha.
- [ ] Preservar o valor bruto e o valor normalizado; não assumir a escala do desgaste sem validação.

## Fase 4 — Persistência assíncrona

- [ ] Criar `LapRecord` imutável e versionado.
- [ ] Criar `CsvLapWriter` como único proprietário do arquivo.
- [ ] Criar o arquivo por sessão, incluindo `session_id`, pista, carro e composto.
- [ ] Gravar setores, combustível, médias de pneus, desgaste, validade e `data_quality`.
- [ ] Usar append, cabeçalho determinístico e codificação UTF-8.
- [ ] Fazer `flush` após cada volta ou lote pequeno, sempre fora do sampler.
- [ ] Reportar falha de disco, fila cheia e eventos perdidos.
- [ ] No shutdown, interromper produção, drenar a fila dentro de um limite e fechar o arquivo.

Se a perda de dados após uma falha do processo for inaceitável, avaliar SQLite ou um journal append-only como armazenamento primário e manter CSV como exportação.

## Fase 5 — Motor de alertas e anti-spam

- [ ] Criar regras determinísticas para combustível, pressão, temperatura, desgaste, bandeiras e danos.
- [ ] Implementar máquina de estados por alerta: `inactive`, `candidate`, `active`, `cooldown`.
- [ ] Aplicar limiares de entrada e saída diferentes para histerese.
- [ ] Associar cada alerta a severidade, código, timestamp e motivo.
- [ ] Deduplicar por código e contexto, não apenas pelo texto.
- [ ] Enviar `AlertEvent` para o áudio sem bloquear a telemetria.

Alertas de segurança e estratégia imediata devem funcionar mesmo se Ollama ou áudio estiverem indisponíveis.

## Fase 6 — Contexto DSL para Ollama

- [ ] Criar `ContextBuilder` que consome somente o snapshot mais recente e o estado derivado.
- [ ] Criar uma versão fixa do DSL, por exemplo `v=1`.
- [ ] Incluir `age_ms`, `data_quality`, unidades e contexto da sessão.
- [ ] Enviar somente no PTT ou em eventos críticos com cooldown.
- [ ] Limitar tamanho do prompt e da resposta.
- [ ] Usar timeout, fila de uma requisição e descarte de contexto obsoleto.
- [ ] Validar a resposta em formato estruturado antes de enviá-la para áudio.
- [ ] Registrar latência, timeout e erro sem gravar telemetria de alta frequência.

O modelo deve explicar ou verbalizar decisões. Ele não deve ser a fonte primária de decisões críticas nem acessar diretamente a memória compartilhada.

## Fase 7 — Interface e ciclo de vida

- [ ] Substituir o laço atual de [`src/main.py`](./src/main.py) por um coordenador de componentes.
- [ ] Remover impressão bloqueante do sampler; criar consumidor de UI com frequência menor.
- [ ] Tratar `KeyboardInterrupt`, exceções de thread e shutdown idempotente.
- [ ] Expor métricas básicas: frequência de amostragem, idade do snapshot, filas, gaps, latência do CSV e Ollama.
- [ ] Criar logs com níveis e rotação, sem escrever no caminho crítico.
- [ ] Permitir execução sem Assetto Corsa usando um replay/mock de snapshots.

## Fase 8 — Testes e validação

### Testes unitários

- [ ] Snapshot consistente e rejeição de pacote alterado.
- [ ] Primeira amostra e rebase de sessão.
- [ ] Uma volta concluída.
- [ ] Voltas duplicadas.
- [ ] Salto de contador de voltas.
- [ ] Combustível crescente por reabastecimento.
- [ ] Amostra ausente ou stale.
- [ ] Derivadas com intervalo irregular.
- [ ] Médias de pneus com pit e pausa.
- [ ] Histerese e cooldown de alertas.
- [ ] Serialização e escaping do CSV.
- [ ] DSL estável e limitado em tamanho.

### Testes de concorrência

- [ ] Consumidor lento não bloqueia o sampler.
- [ ] Fila cheia segue a política definida.
- [ ] Eventos de volta permanecem ordenados.
- [ ] Shutdown encerra todas as threads.
- [ ] Falha do escritor não derruba a aquisição.
- [ ] Timeout do Ollama não bloqueia PTT nem sampler.

### Testes de integração

- [ ] Replay gravado produz os mesmos eventos em execuções repetidas.
- [ ] Processo sobrevive à ausência e ao retorno do Assetto Corsa.
- [ ] CSV pode ser reaberto durante a sessão.
- [ ] Uso de CPU, memória e latência permanecem dentro dos limites definidos.

Executar os testes com o Python do `.venv` e manter o conjunto independente da presença do jogo ou do Ollama.

## Ordem recomendada de implementação

1. Contratos e modelos imutáveis.
2. Leitor seguro do `mmap` e snapshot consistente.
3. Sampler com shutdown, métricas e mock.
4. `StateReducer`, voltas, setores e pneus.
5. Testes de gaps, reset e combustível.
6. Fila de eventos e escritor CSV.
7. Alertas determinísticos e anti-spam.
8. UI desacoplada.
9. DSL, PTT e integração com Ollama.
10. Áudio, telemetria operacional e otimização de desempenho.

## Critérios de conclusão da fundação

- O sampler não faz I/O de disco, rede, áudio ou terminal bloqueante.
- Nenhum consumidor acessa diretamente o `mmap`.
- Cada snapshot possui timestamp, sequência e qualidade.
- Gaps de volta nunca são apresentados como consumo exato.
- Uma falha do CSV ou Ollama não interrompe a aquisição.
- O shutdown fecha threads, filas e mapas de forma previsível.
- Os testes reproduzem resets, perda de amostras, reabastecimento e consumidores lentos.
- A latência e o uso de CPU são medidos, não apenas estimados.
