# 📄 Implementação de Aplicação Elástica na AWS - KCL


### Link do vídeo da aplicação sendo executada:

<https://youtu.be/nmVzdnmKXTA>

---
#### Fase 1: Preparação da Imagem (Golden AMI)

O primeiro passo foi criar um "molde" ou "imagem de ouro" (Golden AMI) da aplicação. Isso garante que cada nova instância provisionada pelo Auto Scaling Group seja idêntica e esteja pronta para receber tráfego.

1.  **Provisionamento da Instância Base:** Uma instância EC2 (tipo `t2.micro`) foi lançada utilizando uma AMI padrão (ex: Amazon Linux 2).
2.  **Instalação da Aplicação:** A aplicação de biblioteca Python e todas as suas dependências (ex: `pip install -r requirements.txt`) foram instaladas e configuradas.
3.  **Configuração do Serviço:** Foi configurado um serviço (ex: via `systemd`) para garantir que a aplicação Python inicie automaticamente junto com o sistema operacional.
4.  **Criação da AMI:** Após validar que a aplicação estava funcional na instância, uma **Amazon Machine Image (AMI)** personalizada foi criada a partir dela. Esta AMI serviu como base para todas as futuras instâncias.

---

#### Fase 2: Configuração do Balanceador de Carga (ALB)

Para distribuir o tráfego de forma eficiente e prover um ponto de acesso único, um Application Load Balancer foi configurado.

1.  **Criação do Load Balancer:** Um ALB (tipo *Application*) foi criado, configurado para ser *internet-facing* e associado às sub-redes públicas (em pelo menos duas Zonas de Disponibilidade para alta disponibilidade).
2.  **Criação do Target Group (Grupo de Destino):** Foi criado um Target Group (tipo *Instance*) para o qual o ALB encaminhará o tráfego.
3.  **Configuração do Health Check:** O Target Group foi configurado com uma verificação de saúde (Health Check) apontando para um endpoint da aplicação (ex: `HTTP /` ou `/health`). O ALB usará isso para saber se uma instância está saudável antes de enviar tráfego para ela.
4.  **Configuração do Listener:** Um *Listener* foi adicionado ao ALB na porta HTTP 80, com a regra padrão de encaminhar (forward) o tráfego para o Target Group criado.

---

#### Fase 3: Configuração do Auto Scaling Group (ASG)

O ASG é o cérebro da elasticidade. Ele foi configurado para gerenciar o ciclo de vida das instâncias EC2.

1.  **Criação do Launch Template (Modelo de Lançamento):** Foi criado um *Launch Template* especificando:
    * A **AMI** personalizada (criada na Fase 1).
    * O **Tipo de Instância** (`t2.micro`, conforme requisito 'a').
    * O **Security Group** (permitindo tráfego apenas do ALB na porta da aplicação).
2.  **Criação do Auto Scaling Group:** Um ASG foi criado utilizando o Launch Template acima.
3.  **Configuração de Rede e Associação ao ALB:** O ASG foi configurado para lançar instâncias nas mesmas sub-redes do ALB e, crucialmente, foi associado ao **Target Group** (criado na Fase 2). Isso garante que qualquer instância nova seja automaticamente registrada no Load Balancer.
4.  **Definição de Tamanho do Grupo (Requisitos 'a' e 'c'):**
    * **Capacidade Desejada (Desired):** 1
    * **Mínimo (Min):** 1
    * **Máximo (Max):** 3

---

#### Fase 4: Definição das Políticas de Elasticidade (CloudWatch)

Finalmente, as regras de negócio para a elasticidade foram implementadas usando alarmes do CloudWatch e políticas de escalonamento.

1.  **Alarme e Política de Scale-Out (Requisito 'c'):**
    * **Alarme (CloudWatch):** Criado o alarme `scale-out-70`.
    * **Métrica:** `CPUUtilization` (Média) do ASG.
    * **Condição:** `> 70%`
    * **Período:** `por 1 minuto` (1 período consecutivo de 60 segundos).
    * **Política (ASG):** Criada uma política do tipo *Step Scaling* associada a este alarme.
    * **Ação:** `Add 1 instance`.

2.  **Alarme e Política de Scale-In (Requisito 'd'):**
    * **Alarme (CloudWatch):** Criado o alarme `scale-in-25`.
    * **Métrica:** `CPUUtilization` (Média) do ASG.
    * **Condição:** `< 25%`
    * **Período:** `por 1 minuto` (1 período consecutivo de 60 segundos).
    * **Política (ASG):** Criada uma política do tipo *Step Scaling* associada a este alarme.
    * **Ação:** `Remove 1 instance`.


### 4. Validação e Testes

Para validar a arquitetura, foram realizados testes de carga simulados:

1.  **Teste de Scale-Out:** Foi utilizada uma ferramenta de stress de CPU (ex: `stress-ng` ou um script de loop infinito) em uma das instâncias para forçar a média de CPU do grupo a ultrapassar 70%.
    * **Resultado Esperado:** O alarme `scale-out-70` disparou, o ASG iniciou uma nova instância (até o máximo de 3). A nova instância foi registrada no ALB e começou a receber tráfego, diluindo a carga.
2.  **Teste de Scale-In:** O teste de carga foi interrompido. A utilização de CPU caiu.
    * **Resultado Esperado:** Após a média de CPU do grupo ficar abaixo de 25% por 1 minuto, o alarme `scale-in-25` disparou, e o ASG finalizou uma das instâncias (até o mínimo de 1).
