// ============================================================
// Research Domain Enquirer — Comprehensive Mock Data
// ============================================================

import type {
  QueryResponse,
  GraphData,
  Paper,
  PaperChunk,
  EvaluationMetrics,
  IngestionStatus,
  EntityDetails,
} from '../types';

// --- Mock Query Response ------------------------------------

export const MOCK_QUERY_RESPONSE: QueryResponse = {
  queryId: 'q-mock-001',
  query: 'How does LoRA compare to full fine-tuning on LLMs?',
  answer: `LoRA (Low-Rank Adaptation) significantly reduces memory requirements compared to full fine-tuning [2106.09685]. Specifically, LoRA reduces trainable parameters by up to **10,000×** while achieving comparable performance on GLUE and SuperGLUE benchmarks [2106.09685].

The key insight behind LoRA is that weight updates during adaptation have a **low intrinsic rank** [2106.09685]. Instead of updating the full weight matrix W ∈ ℝ^{d×k}, LoRA decomposes the update into two low-rank matrices: ΔW = BA where B ∈ ℝ^{d×r} and A ∈ ℝ^{r×k}, with rank r ≪ min(d, k).

In benchmarks using GPT-3 175B, LoRA matches or exceeds the quality of full fine-tuning on GLUE while using only 0.01% of the parameters that would otherwise require gradient updates [2106.09685]. QLoRA further extends this by quantizing the base model to 4-bit precision [2305.14314], enabling fine-tuning of 65B parameter models on a single 48GB GPU [2305.14314].

Compared to other parameter-efficient methods like prefix tuning and adapter layers, LoRA introduces **no additional inference latency** because the low-rank matrices can be merged back into the original weights [2106.09685]. Full fine-tuning on the other hand requires storing and loading separate model checkpoints for each downstream task [2212.09561].`,
  confidence: 0.91,
  confidenceLevel: 'high',
  action: 'PASS',
  citations: [
    {
      paperId: '2106.09685',
      title: 'LoRA: Low-Rank Adaptation of Large Language Models',
      authors: ['Hu, Edward J.', 'Shen, Yelong', 'Wallis, Phillip', 'Allen-Zhu, Zeyuan', 'Li, Yuanzhi', 'Wang, Shean', 'Chen, Weizhu'],
      year: 2021,
      url: 'https://arxiv.org/abs/2106.09685',
      relevanceScore: 0.98,
    },
    {
      paperId: '2305.14314',
      title: 'QLoRA: Efficient Finetuning of Quantized LLMs',
      authors: ['Dettmers, Tim', 'Pagnoni, Artidoro', 'Holtzman, Ari', 'Zettlemoyer, Luke'],
      year: 2023,
      url: 'https://arxiv.org/abs/2305.14314',
      relevanceScore: 0.87,
    },
    {
      paperId: '2212.09561',
      title: 'Scaling Instruction-Finetuned Language Models',
      authors: ['Chung, Hyung Won', 'Hou, Le', 'Longpre, Shayne'],
      year: 2022,
      url: 'https://arxiv.org/abs/2212.09561',
      relevanceScore: 0.74,
    },
    {
      paperId: '2101.03961',
      title: 'The Power of Scale for Parameter-Efficient Prompt Tuning',
      authors: ['Lester, Brian', 'Al-Rfou, Rami', 'Constant, Noah'],
      year: 2021,
      url: 'https://arxiv.org/abs/2101.03961',
      relevanceScore: 0.68,
    },
  ],
  chunks: [
    {
      chunkId: 'c-001',
      paperId: '2106.09685',
      paperTitle: 'LoRA: Low-Rank Adaptation of Large Language Models',
      section: 'Abstract',
      text: 'We propose Low-Rank Adaptation, or LoRA, which freezes the pretrained model weights and injects trainable rank decomposition matrices into each layer of the Transformer architecture, greatly reducing the number of trainable parameters for downstream tasks.',
      relevanceScore: 1.0,
      pageNumber: 1,
    },
    {
      chunkId: 'c-002',
      paperId: '2106.09685',
      paperTitle: 'LoRA: Low-Rank Adaptation of Large Language Models',
      section: 'Introduction',
      text: 'LoRA reduces the number of trainable parameters by 10,000 times and the GPU memory requirement by 3 times compared to full fine-tuning in GPT-3 (175B) while performing on-par or better than full fine-tuning measured by both model quality and practical throughput.',
      relevanceScore: 0.97,
      pageNumber: 2,
    },
    {
      chunkId: 'c-003',
      paperId: '2106.09685',
      paperTitle: 'LoRA: Low-Rank Adaptation of Large Language Models',
      section: '§4 Experiments',
      text: 'We compare LoRA to several baselines, including full fine-tuning, prefix-tuning, and adapter layers. LoRA matches or exceeds all baselines on GLUE and SuperGLUE while being significantly more parameter-efficient.',
      relevanceScore: 0.93,
      pageNumber: 6,
    },
    {
      chunkId: 'c-004',
      paperId: '2305.14314',
      paperTitle: 'QLoRA: Efficient Finetuning of Quantized LLMs',
      section: 'Abstract',
      text: 'QLoRA backpropagates gradients through a frozen, 4-bit quantized pretrained language model into Low Rank Adapters (LoRA). We use QLoRA to fine-tune more than 1,000 models, providing a detailed analysis of instruction following and chatbot performance across many model types.',
      relevanceScore: 0.87,
      pageNumber: 1,
    },
    {
      chunkId: 'c-005',
      paperId: '2106.09685',
      paperTitle: 'LoRA: Low-Rank Adaptation of Large Language Models',
      section: '§2 Problem Statement',
      text: 'We hypothesize that the change in weights during model adaptation also has a low "intrinsic rank", leading to our proposed Low-Rank Adaptation (LoRA) approach.',
      relevanceScore: 0.85,
      pageNumber: 3,
    },
    {
      chunkId: 'c-006',
      paperId: '2212.09561',
      paperTitle: 'Scaling Instruction-Finetuned Language Models',
      section: '§3 Experiments',
      text: 'Full fine-tuning consistently outperforms instruction tuning in low-resource settings but requires substantially more compute, memory, and storage. Parameter-efficient methods provide a compelling alternative.',
      relevanceScore: 0.81,
      pageNumber: 8,
    },
    {
      chunkId: 'c-007',
      paperId: '2305.14314',
      paperTitle: 'QLoRA: Efficient Finetuning of Quantized LLMs',
      section: '§4 Guanaco: A Chatbot Built with QLoRA',
      text: 'Using QLoRA, we can fine-tune a 65B parameter model on a single 48GB GPU, achieving performance competitive with ChatGPT on the Vicuna benchmark.',
      relevanceScore: 0.79,
      pageNumber: 7,
    },
    {
      chunkId: 'c-008',
      paperId: '2101.03961',
      paperTitle: 'The Power of Scale for Parameter-Efficient Prompt Tuning',
      section: 'Abstract',
      text: 'As model size grows, prefix tuning closes the performance gap with full fine-tuning. At the scale of T5-XXL, prompt tuning matches the performance of full fine-tuning on SuperGLUE.',
      relevanceScore: 0.71,
      pageNumber: 1,
    },
    {
      chunkId: 'c-009',
      paperId: '2106.09685',
      paperTitle: 'LoRA: Low-Rank Adaptation of Large Language Models',
      section: '§6 Related Work',
      text: 'Unlike adapter layers, LoRA does not introduce inference latency, as the trained low-rank matrices can be merged with the frozen weights during inference.',
      relevanceScore: 0.69,
      pageNumber: 9,
    },
    {
      chunkId: 'c-010',
      paperId: '2212.09561',
      paperTitle: 'Scaling Instruction-Finetuned Language Models',
      section: '§5 Conclusion',
      text: 'Fine-tuning with LoRA provides an efficient path for adapting large models to new tasks, particularly when combined with careful data curation and instruction formatting.',
      relevanceScore: 0.63,
      pageNumber: 14,
    },
  ],
  verification: {
    overallConfidence: 0.91,
    action: 'PASS',
    claimsExtracted: 4,
    supported: 3,
    partiallySupported: 1,
    unsupported: 0,
    contradicted: 0,
    evidenceCoverage: 0.85,
    citationAccuracy: 1.0,
    claims: [
      {
        id: 'cl-1',
        text: 'LoRA reduces trainable parameters by approximately 10,000×',
        status: 'SUPPORTED',
        confidence: 0.98,
        evidence: 'LoRA reduces the number of trainable parameters by 10,000 times and the GPU memory requirement by 3 times compared to full fine-tuning in GPT-3 (175B)...',
        sourceRef: '[2106.09685] Introduction',
      },
      {
        id: 'cl-2',
        text: 'LoRA matches full fine-tuning quality on GLUE (GPT-3)',
        status: 'SUPPORTED',
        confidence: 0.94,
        evidence: 'LoRA matches or exceeds all baselines on GLUE and SuperGLUE while being significantly more parameter-efficient.',
        sourceRef: '[2106.09685] §4 Experiments',
      },
      {
        id: 'cl-3',
        text: 'LoRA introduces no additional inference latency',
        status: 'SUPPORTED',
        confidence: 0.96,
        evidence: 'Unlike adapter layers, LoRA does not introduce inference latency, as the trained low-rank matrices can be merged with the frozen weights during inference.',
        sourceRef: '[2106.09685] §6 Related Work',
      },
      {
        id: 'cl-4',
        text: 'QLoRA enables fine-tuning of 65B models on a single GPU',
        status: 'PARTIALLY_SUPPORTED',
        confidence: 0.72,
        evidence: 'Using QLoRA, we can fine-tune a 65B parameter model on a single 48GB GPU...',
        sourceRef: '[2305.14314] §4 Guanaco',
      },
    ],
  },
  latencyMs: 2340,
  timestamp: new Date().toISOString(),
};

// --- Mock Graph Data ----------------------------------------

export const MOCK_GRAPH_DATA: GraphData = {
  nodes: [
    // Papers
    { id: '2106.09685', label: 'LoRA (2021)', type: 'Paper', year: 2021 },
    { id: '2305.14314', label: 'QLoRA (2023)', type: 'Paper', year: 2023 },
    { id: '2212.09561', label: 'Flan (2022)', type: 'Paper', year: 2022 },
    { id: '2101.03961', label: 'Prefix Tuning (2021)', type: 'Paper', year: 2021 },
    { id: '2005.14165', label: 'GPT-3 (2020)', type: 'Paper', year: 2020 },
    { id: '1706.03762', label: 'Attention Is All You Need (2017)', type: 'Paper', year: 2017 },
    { id: '2302.13971', label: 'LLaMA (2023)', type: 'Paper', year: 2023 },
    { id: '2307.09288', label: 'LLaMA 2 (2023)', type: 'Paper', year: 2023 },
    { id: '2210.11610', label: 'AdaLoRA (2023)', type: 'Paper', year: 2023 },
    { id: '2309.12307', label: 'DoRA (2024)', type: 'Paper', year: 2024 },
    // Models
    { id: 'gpt3', label: 'GPT-3', type: 'Model' },
    { id: 'llama2', label: 'LLaMA-2', type: 'Model' },
    { id: 'roberta', label: 'RoBERTa', type: 'Model' },
    { id: 'bert', label: 'BERT', type: 'Model' },
    { id: 'gpt4', label: 'GPT-4', type: 'Model' },
    // Methods
    { id: 'lora', label: 'LoRA', type: 'Method' },
    { id: 'qlora', label: 'QLoRA', type: 'Method' },
    { id: 'adalora', label: 'AdaLoRA', type: 'Method' },
    { id: 'dora', label: 'DoRA', type: 'Method' },
    { id: 'prefix-tuning', label: 'Prefix Tuning', type: 'Method' },
    { id: 'prompt-tuning', label: 'Prompt Tuning', type: 'Method' },
    { id: 'adapter', label: 'Adapter Layers', type: 'Method' },
    { id: 'full-ft', label: 'Full Fine-Tuning', type: 'Method' },
    // Datasets
    { id: 'glue', label: 'GLUE', type: 'Dataset' },
    { id: 'superglue', label: 'SuperGLUE', type: 'Dataset' },
    { id: 'alpaca', label: 'Alpaca Dataset', type: 'Dataset' },
    { id: 'openorca', label: 'OpenOrca', type: 'Dataset' },
    // Concepts
    { id: 'transformer', label: 'Transformer Architecture', type: 'Concept' },
    { id: 'peft', label: 'Parameter-Efficient Fine-Tuning', type: 'Concept' },
    { id: 'attention', label: 'Self-Attention', type: 'Concept' },
    { id: 'quantization', label: '4-bit Quantization', type: 'Concept' },
    // Benchmarks
    { id: 'mt-bench', label: 'MT-Bench', type: 'Benchmark' },
    { id: 'humaneval', label: 'HumanEval', type: 'Benchmark' },
    { id: 'mmlu', label: 'MMLU', type: 'Benchmark' },
  ],
  edges: [
    // Paper introduces method
    { id: 'e1', source: '2106.09685', target: 'lora', type: 'INTRODUCES' },
    { id: 'e2', source: '2305.14314', target: 'qlora', type: 'INTRODUCES' },
    { id: 'e3', source: '2210.11610', target: 'adalora', type: 'INTRODUCES' },
    { id: 'e4', source: '2309.12307', target: 'dora', type: 'INTRODUCES' },
    { id: 'e5', source: '2101.03961', target: 'prefix-tuning', type: 'INTRODUCES' },
    { id: 'e6', source: '1706.03762', target: 'transformer', type: 'PROPOSES' },
    // Method improves / extends
    { id: 'e7', source: 'qlora', target: 'lora', type: 'EXTENDS' },
    { id: 'e8', source: 'adalora', target: 'lora', type: 'EXTENDS' },
    { id: 'e9', source: 'dora', target: 'lora', type: 'EXTENDS' },
    { id: 'e10', source: 'lora', target: 'adapter', type: 'IMPROVES' },
    { id: 'e11', source: 'lora', target: 'peft', type: 'PART_OF' },
    { id: 'e12', source: 'qlora', target: 'quantization', type: 'USES' },
    // Papers cite papers
    { id: 'e13', source: '2305.14314', target: '2106.09685', type: 'CITES' },
    { id: 'e14', source: '2210.11610', target: '2106.09685', type: 'CITES' },
    { id: 'e15', source: '2309.12307', target: '2106.09685', type: 'CITES' },
    { id: 'e16', source: '2106.09685', target: '1706.03762', type: 'CITES' },
    { id: 'e17', source: '2302.13971', target: '1706.03762', type: 'CITES' },
    { id: 'e18', source: '2307.09288', target: '2302.13971', type: 'CITES' },
    { id: 'e19', source: '2305.14314', target: '2302.13971', type: 'CITES' },
    { id: 'e20', source: '2106.09685', target: '2005.14165', type: 'CITES' },
    // Evaluates on datasets/benchmarks
    { id: 'e21', source: '2106.09685', target: 'glue', type: 'EVALUATES_ON' },
    { id: 'e22', source: '2106.09685', target: 'superglue', type: 'EVALUATES_ON' },
    { id: 'e23', source: '2305.14314', target: 'mt-bench', type: 'EVALUATES_ON' },
    { id: 'e24', source: '2307.09288', target: 'mmlu', type: 'EVALUATES_ON' },
    { id: 'e25', source: '2307.09288', target: 'humaneval', type: 'EVALUATES_ON' },
    // Method uses concept
    { id: 'e26', source: 'lora', target: 'transformer', type: 'BASED_ON' },
    { id: 'e27', source: 'prefix-tuning', target: 'attention', type: 'BASED_ON' },
    { id: 'e28', source: 'transformer', target: 'attention', type: 'PART_OF' },
    // Model evaluated on dataset
    { id: 'e29', source: '2005.14165', target: 'gpt3', type: 'INTRODUCES' },
    { id: 'e30', source: '2302.13971', target: 'llama2', type: 'INTRODUCES' },
    { id: 'e31', source: 'gpt3', target: 'glue', type: 'EVALUATES_ON' },
    { id: 'e32', source: 'llama2', target: 'mmlu', type: 'EVALUATES_ON' },
    { id: 'e33', source: '2212.09561', target: 'glue', type: 'EVALUATES_ON' },
    // Fine-tuning methods
    { id: 'e34', source: 'prompt-tuning', target: 'peft', type: 'PART_OF' },
    { id: 'e35', source: 'adapter', target: 'peft', type: 'PART_OF' },
    { id: 'e36', source: 'prefix-tuning', target: 'peft', type: 'PART_OF' },
    // Training data
    { id: 'e37', source: '2305.14314', target: 'alpaca', type: 'USES' },
    { id: 'e38', source: '2212.09561', target: 'openorca', type: 'USES' },
    // Related
    { id: 'e39', source: 'lora', target: 'prompt-tuning', type: 'RELATED_TO' },
    { id: 'e40', source: 'full-ft', target: 'peft', type: 'RELATED_TO' },
    { id: 'e41', source: '2106.09685', target: 'roberta', type: 'EVALUATES_ON' },
    { id: 'e42', source: '2106.09685', target: 'bert', type: 'RELATED_TO' },
    { id: 'e43', source: 'bert', target: 'glue', type: 'EVALUATES_ON' },
    { id: 'e44', source: 'roberta', target: 'glue', type: 'EVALUATES_ON' },
    { id: 'e45', source: 'qlora', target: 'openorca', type: 'USES' },
    { id: 'e46', source: 'adalora', target: 'glue', type: 'EVALUATES_ON' },
    { id: 'e47', source: 'dora', target: 'mt-bench', type: 'EVALUATES_ON' },
    { id: 'e48', source: '2210.11610', target: '2101.03961', type: 'CITES' },
    { id: 'e49', source: '2309.12307', target: '2305.14314', type: 'CITES' },
    { id: 'e50', source: '2307.09288', target: 'alpaca', type: 'USES' },
    { id: 'e51', source: 'gpt4', target: 'mt-bench', type: 'EVALUATES_ON' },
    { id: 'e52', source: 'gpt4', target: 'mmlu', type: 'EVALUATES_ON' },
  ],
};

// --- Mock Paper (LoRA) ---------------------------------------

export const MOCK_PAPER: Paper = {
  paperId: '2106.09685',
  arxivId: '2106.09685',
  title: 'LoRA: Low-Rank Adaptation of Large Language Models',
  authors: ['Hu, Edward J.', 'Shen, Yelong', 'Wallis, Phillip', 'Allen-Zhu, Zeyuan', 'Li, Yuanzhi', 'Wang, Shean', 'Wang, Lu', 'Chen, Weizhu'],
  publishedDate: '2021-06-17',
  categories: ['cs.CL', 'cs.LG', 'cs.AI'],
  abstract: `An important paradigm of natural language processing consists of large-scale pre-training on general domain data and adaptation to particular tasks or domains. As we pre-train larger models, full fine-tuning, which retrains all model parameters, becomes less feasible. Using GPT-3 175B as an example -- deploying independent instances of fine-tuned models, each with 175B parameters, is prohibitively expensive. We propose Low-Rank Adaptation, or LoRA, which freezes the pretrained model weights and injects trainable rank decomposition matrices into each layer of the Transformer architecture, greatly reducing the number of trainable parameters for downstream tasks. Compared to GPT-3 175B fine-tuned with Adam, LoRA can reduce the number of trainable parameters by 10,000 times and the GPU memory requirement by 3 times. LoRA performs on-par or better than fine-tuning in model quality on RoBERTa, DeBERTa, GPT-2, and GPT-3, despite having fewer trainable parameters, a higher training throughput, and, unlike adapters, no additional inference latency. We also provide an empirical investigation into the rank-deficiency in language model adaptation, which sheds light on the efficacy of LoRA.`,
  pdfUrl: 'https://arxiv.org/pdf/2106.09685',
  entities: [
    { id: 'lora', name: 'LoRA', type: 'Method', role: 'PROPOSES' },
    { id: 'gpt3', name: 'GPT-3', type: 'Model', role: 'EVALUATES_ON' },
    { id: 'gpt2', name: 'GPT-2', type: 'Model', role: 'EVALUATES_ON' },
    { id: 'roberta', name: 'RoBERTa', type: 'Model', role: 'EVALUATES_ON' },
    { id: 'deberta', name: 'DeBERTa', type: 'Model', role: 'EVALUATES_ON' },
    { id: 'glue', name: 'GLUE', type: 'Dataset', role: 'EVALUATES_ON' },
    { id: 'superglue', name: 'SuperGLUE', type: 'Dataset', role: 'EVALUATES_ON' },
    { id: 'e2nlg', name: 'E2E NLG', type: 'Benchmark', role: 'EVALUATES_ON' },
    { id: 'wikisql', name: 'WikiSQL', type: 'Dataset', role: 'EVALUATES_ON' },
    { id: 'webq', name: 'WebNLG', type: 'Dataset', role: 'EVALUATES_ON' },
    { id: 'transformer', name: 'Transformer', type: 'Concept', role: 'BASED_ON' },
    { id: 'adapter', name: 'Adapter Layers', type: 'Method', role: 'RELATED_TO' },
  ],
  citationCount: 8420,
  totalChunks: 47,
};

export const MOCK_PAPER_CHUNKS: PaperChunk[] = [
  {
    chunkId: 'pc-001',
    section: 'Abstract',
    text: 'We propose Low-Rank Adaptation, or LoRA, which freezes the pretrained model weights and injects trainable rank decomposition matrices into each layer of the Transformer architecture, greatly reducing the number of trainable parameters for downstream tasks.',
    pageNumber: 1,
    relevanceScore: 0.98,
  },
  {
    chunkId: 'pc-002',
    section: '§1 Introduction',
    text: 'We hypothesize that the change in weights during model adaptation also has a low "intrinsic rank", leading to our proposed Low-Rank Adaptation (LoRA) approach. For a pre-trained weight matrix W0 ∈ ℝ^{d×k}, we constrain its update by representing the latter with a low-rank decomposition.',
    pageNumber: 2,
    relevanceScore: 0.91,
  },
  {
    chunkId: 'pc-003',
    section: '§4 Experiments',
    text: 'Table 2 shows the validation accuracy on a subset of GLUE tasks. LoRA matches or exceeds the performance of Adapter and Prefix Tuning in most tasks with fewer trainable parameters, demonstrating the efficiency of our approach.',
    pageNumber: 6,
    relevanceScore: 0.88,
  },
  {
    chunkId: 'pc-004',
    section: '§6 Related Work',
    text: 'Unlike adapter layers, LoRA does not introduce inference latency, as the trained low-rank matrices can be merged with the frozen weights during inference. This is a significant practical advantage when deploying multiple task-specific models.',
    pageNumber: 9,
    relevanceScore: 0.82,
  },
  {
    chunkId: 'pc-005',
    section: '§7 Conclusion',
    text: 'We propose LoRA, an efficient adaptation strategy that neither introduces inference latency nor reduces input sequence length while retaining high model quality. LoRA can be applied to any neural networks with dense layers.',
    pageNumber: 11,
    relevanceScore: 0.76,
  },
];

// --- Mock Entity Details -------------------------------------

export const MOCK_ENTITY_DETAILS: EntityDetails = {
  id: 'lora',
  name: 'LoRA',
  type: 'Method',
  description: 'Low-Rank Adaptation — a parameter-efficient fine-tuning technique that freezes pretrained model weights and injects trainable low-rank decomposition matrices.',
  year: 2021,
  paperCount: 247,
  relatedEntities: [
    { id: 'qlora', label: 'QLoRA', type: 'Method', relation: 'EXTENDS' },
    { id: 'adalora', label: 'AdaLoRA', type: 'Method', relation: 'EXTENDS' },
    { id: 'dora', label: 'DoRA', type: 'Method', relation: 'EXTENDS' },
    { id: 'glue', label: 'GLUE', type: 'Dataset', relation: 'EVALUATES_ON' },
    { id: 'superglue', label: 'SuperGLUE', type: 'Dataset', relation: 'EVALUATES_ON' },
    { id: '2106.09685', label: 'LoRA Paper', type: 'Paper', relation: 'INTRODUCES' },
    { id: 'transformer', label: 'Transformer', type: 'Concept', relation: 'BASED_ON' },
    { id: 'peft', label: 'PEFT', type: 'Concept', relation: 'PART_OF' },
  ],
  properties: {
    trainableParams: '0.1% of full model',
    rank: '4-64',
    inferenceOverhead: 'None',
    mergeWeights: true,
  },
};

// --- Mock Evaluation Metrics ---------------------------------

const generateRecallTrend = () => {
  const trend = [];
  const baseDate = new Date('2024-01-01');
  for (let i = 0; i < 14; i++) {
    const date = new Date(baseDate);
    date.setDate(date.getDate() + i);
    trend.push({
      date: date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      recall5: parseFloat((0.74 + Math.random() * 0.06).toFixed(3)),
      recall10: parseFloat((0.82 + Math.random() * 0.04).toFixed(3)),
    });
  }
  return trend;
};

export const MOCK_EVALUATION_METRICS: EvaluationMetrics = {
  runId: 'eval-2024-0115-02',
  timestamp: '2024-01-15T02:14:00Z',
  retrieval: {
    recall10:  { name: 'Recall@10',   value: 0.84, target: 0.80, delta7d: +0.03, pass: true },
    mrr:       { name: 'MRR',         value: 0.79, target: 0.75, delta7d: -0.01, pass: true },
    hitRate10: { name: 'Hit Rate@10', value: 0.92, target: 0.90, delta7d: +0.02, pass: true },
    ndcg10:    { name: 'nDCG@10',     value: 0.81, target: 0.75, delta7d: +0.01, pass: true },
  },
  generation: {
    faithfulness:    { name: 'Faithfulness',    value: 0.91, target: 0.85, delta7d: +0.02, pass: true },
    groundedness:    { name: 'Groundedness',    value: 0.85, target: 0.80, delta7d: +0.03, pass: true },
    citationAccuracy:{ name: 'Citation Acc.',   value: 0.97, target: 0.95, delta7d:  0.00, pass: true },
    relevance:       { name: 'Relevance',       value: 0.88, target: 0.80, delta7d: +0.05, pass: true },
  },
  latency: {
    p50: 2.1,
    p90: 3.8,
    p95: 4.2,
    p99: 7.8,
    timestamp: '2024-01-15T02:14:00Z',
  },
  confidenceDistribution: {
    high: 73,
    medium: 21,
    low: 5,
    refused: 1,
  },
  recallTrend: generateRecallTrend(),
};

// --- Mock Ingestion Status -----------------------------------

export const MOCK_INGESTION_STATUS: IngestionStatus = {
  papersIndexed: 8742,
  newToday: 147,
  failedToday: 2,
  entitiesInGraph: 24891,
  edgesInGraph: 187342,
  lastFetchUtc: '2024-01-15T12:00:00Z',
  nextFetchUtc: '2024-01-15T18:00:00Z',
  paperQueueDepth: 0,
  dlqDepth: 2,
  status: 'healthy',
};
