"""DINOv2 (ViT-S/14) + faiss kNN 异常检测 —— 冻结编码器、即录即用。

架构: 冻结的 DINOv2 ViT-S/14 提 384 维 CLS embedding (L2 归一化)
      faiss IndexFlatL2 索引向量; kNN (k=5) 投票判 0/1 + 子类。

与 printguard 原型网络同口径 (zero / adapted 双轨, 适配轨 --adapt-weight 加权):
  - zero    : 索引只用公开数据 (benchmark/train)
  - adapted : 索引加入 bambu_train, 每张复制 adapt_weight 份以加权
              (等价于 kNN 票面上让适配图密度提升 adapt_weight 倍)
"""
import os
import sys
import torch
import torch.nn as nn
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# 发布包内置副本: dinov2_cache 就在部署目录内, 不再依赖研究仓 baselines/ 结构
BASE = HERE
sys.path.insert(0, HERE)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# 走本地 hub 仓库路径, 避免 torch.hub 去 github 验网络
DINOV2_HUB_LOCAL = os.path.join(BASE, "dinov2_cache", "hub", "facebookresearch_dinov2_main")
DINOV2_CKPT_LOCAL = os.path.join(BASE, "dinov2_cache", "hub", "checkpoints",
                                 "dinov2_vits14_pretrain.pth")
DINOV2_NAME = "dinov2_vits14"
EMBED_DIM = 384
K = 5

# ImageNet 归一化 + 224 输入 (DINOv2 要求, 14 的整数倍)
from torchvision import transforms
TFM = transforms.Compose([
    transforms.Resize(256, interpolation=transforms.InterpolationMode.BICUBIC),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
])

_MODEL = None


def load_model():
    """懒加载+缓存 DINOv2 ViT-S/14, 冻结参数。走本地 hub 仓库 + 本地权重, 零网络依赖。"""
    global _MODEL
    if _MODEL is None:
        if not os.path.isdir(DINOV2_HUB_LOCAL):
            raise FileNotFoundError(
                f"本地 DINOv2 hub 仓库缺失: {DINOV2_HUB_LOCAL}\n"
                f"首次运行: 会从 torch.hub 拉取 (需联网), 后续将走本地缓存。")
        if not os.path.exists(DINOV2_CKPT_LOCAL):
            raise FileNotFoundError(f"本地权重缺失: {DINOV2_CKPT_LOCAL}")
        model = torch.hub.load(DINOV2_HUB_LOCAL, DINOV2_NAME,
                               source="local", pretrained=False)
        sd = torch.load(DINOV2_CKPT_LOCAL, map_location="cpu", weights_only=True)
        model.load_state_dict(sd)
        model.eval().to(DEVICE)
        for p in model.parameters():
            p.requires_grad = False
        _MODEL = model
    return _MODEL


@torch.no_grad()
def extract(paths, bs=32):
    """多图并行提 CLS embedding -> L2 归一化 [N, 384] numpy。"""
    from PIL import Image
    from torch.utils.data import DataLoader, TensorDataset

    model = load_model()
    tensors = [TFM(Image.open(p).convert("RGB")) for p in paths]
    if not tensors:
        return np.zeros((0, EMBED_DIM), dtype="float32")
    x = torch.stack(tensors)
    feats = []
    for i in range(0, len(x), bs):
        chunk = x[i:i + bs].to(DEVICE)
        f = model(chunk)  # [B, 384] CLS token
        feats.append(f.cpu())
    z = torch.cat(feats, 0)
    z = torch.nn.functional.normalize(z, dim=1)
    return z.numpy().astype("float32")


def build_index(emb_public, labels_public, emb_extra=None, extra_weight=20):
    """建 faiss IndexFlatL2。emb_extra 每行复制 extra_weight 份以加权。
    返回 (index, labels_array)  其中 labels 是子类名 (normal / anomaly 子类)。
    """
    if emb_extra is not None and len(emb_extra) > 0:
        # 每张适配图复制 extra_weight 份 -> kNN 票面加权
        emb_extra_w = np.repeat(emb_extra, extra_weight, axis=0)
        labels_extra_w = []
        # 需要传子类标签, 这里用 label_dict 形式: labels 是 string 列表
        # 约定: caller 传入的 labels_public 已经是 string 子类列表
        # emb_extra 的子类标签调用方在 build_index 调用前应已 concat
        raise ValueError("Use build_index_from_labels instead")
    raise ValueError("Use build_index_from_labels instead")


def build_index_from_labels(emb, labels, extra_emb=None, extra_labels=None,
                            extra_weight=20):
    """emb: [N,384] L2 归一化; labels: list[str] 同长度子类名。
    extra_* 同上, 适配图将按 extra_weight 倍数重复 add 到索引中。
    仅研究/建库路线使用; 运行时检测走 knn.py 的 numpy 实现, 无需 faiss。
    """
    import faiss  # 惰性导入: 发布环境不装 faiss 也能跑检测
    parts_e = [emb]
    parts_l = list(labels)
    if extra_emb is not None and len(extra_emb) > 0:
        parts_e.append(np.repeat(extra_emb, extra_weight, axis=0))
        parts_l.extend(extra_labels * int(extra_weight))
    E = np.concatenate(parts_e, axis=0).astype("float32")
    L = np.array(parts_l)
    index = faiss.IndexFlatL2(E.shape[1])
    index.add(E)
    return index, L


def predict(index, labels, query_emb, k=K):
    """单图 embedding -> (label_int, sub_cls_str)
    label_int: 0=normal, 1=anomaly
    投票: k 个近邻里各子类出现次数, normal 总票数最多的判 0, 否则 1+取最频繁子类
    """
    if query_emb.ndim == 1:
        query_emb = query_emb[None, :]
    _, idxs = index.search(query_emb.astype("float32"), k)
    nn_labels = labels[idxs[0]]
    # 按子类计票
    from collections import Counter
    cnt = Counter(nn_labels.tolist())
    sub_pred, _ = cnt.most_common(1)[0]
    label_int = 0 if sub_pred == "normal" else 1
    return label_int, sub_pred


def save_index(index, labels, path):
    import faiss
    faiss.write_index(index, path + ".faiss")
    # labels 用 utf-8 文本逐行存, 不走 pickle, 避开 numpy allow_pickle 反序列化风险
    with open(path + ".labels.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(str(x) for x in labels))


def load_index(path):
    import faiss
    index = faiss.read_index(path + ".faiss")
    with open(path + ".labels.txt", "r", encoding="utf-8") as f:
        labels = np.array([ln.rstrip("\n") for ln in f])
    return index, labels


# ============ 原型匹配 (类均值向量) ============

def build_prototypes(emb, labels, extra_emb=None, extra_labels=None,
                     extra_weight=10.0):
    """计算每个子类的加权均值原型。
    emb/labels: 公开数据; extra_*: 适配数据 (每张复制 extra_weight 份以加权)。
    返回 protos: [N_class, 384] numpy, proto_names: 与 protos 行对应的子类名列表。
    """
    from collections import defaultdict
    sums, counts = defaultdict(lambda: np.zeros(EMBED_DIM, dtype="float64")), defaultdict(int)
    for z, c in zip(emb, labels):
        sums[c] += z
        counts[c] += 1
    if extra_emb is not None and len(extra_emb) > 0:
        w = int(extra_weight)
        for z, c in zip(extra_emb, extra_labels):
            sums[c] += z * w
            counts[c] += w
    proto_names = sorted(sums.keys(), key=lambda s: (s != "normal", s))
    protos = np.stack([sums[c] / counts[c] for c in proto_names]).astype("float32")
    # L2 归一化原型, 后续预测也用归一化后点积近似余弦
    norms = np.linalg.norm(protos, axis=1, keepdims=True) + 1e-12
    protos = protos / norms
    return protos, proto_names


def predict_proto(protos, proto_names, query_emb):
    """单图 embedding -> (label_int, sub_cls_str)
    label_int: 0=normal, 1=anomaly
    取最近原型类 (L2 距离等价于余弦距离, 因已归一化)。
    """
    if query_emb.ndim == 1:
        query_emb = query_emb[None, :]
    # 归一化 query 后走 L2 距离
    q = query_emb / (np.linalg.norm(query_emb, axis=1, keepdims=True) + 1e-12)
    d = ((protos - q) ** 2).sum(1)
    idx = int(d.argmin())
    sub_pred = proto_names[idx]
    label_int = 0 if sub_pred == "normal" else 1
    return label_int, sub_pred


def save_prototypes(protos, proto_names, path):
    np.save(path + ".protos.npy", protos)
    with open(path + ".labels.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(str(x) for x in proto_names))


def load_prototypes(path):
    protos = np.load(path + ".protos.npy")
    with open(path + ".labels.txt", "r", encoding="utf-8") as f:
        names = np.array([ln.rstrip("\n") for ln in f])
    return protos, names


# ============ 线性探针头 (DINOv2 特征之上的二分类微调) ============

class _Head(nn.Module):
    def __init__(self, in_dim=EMBED_DIM, n_cls=2):
        super().__init__()
        self.fc = nn.Linear(in_dim, n_cls)
    def forward(self, z):
        return self.fc(z)


def _to_xy(emb, labels, extra_emb=None, extra_labels=None, extra_weight=10.0):
    """拼接样本与适配样本, 返回 (X_tensor, y_tensor, sample_weight_tensor)。
    label 0=normal, 1=anomaly; 适配图复制 extra_weight 份以加权."""
    def lab2y(s):
        return 0 if s == "normal" else 1
    X = [torch.from_numpy(emb).float()]
    y = [torch.tensor([lab2y(c) for c in labels], dtype=torch.long)]
    w = [torch.ones(len(emb))]
    if extra_emb is not None and len(extra_emb) > 0:
        X.append(torch.from_numpy(np.repeat(extra_emb, int(extra_weight), axis=0)).float())
        y.append(torch.tensor([lab2y(c) for c in extra_labels] * int(extra_weight), dtype=torch.long))
        w.append(torch.full((len(extra_emb) * int(extra_weight),), float(extra_weight)))
    return torch.cat(X, 0), torch.cat(y, 0), torch.cat(w, 0)


def train_linear_head(emb, labels, extra_emb=None, extra_labels=None,
                      extra_weight=10.0, epochs=10, lr=1e-3, bs=64,
                      device=DEVICE, seed=42):
    """线性探针: DINOv2 冻结特征之上训 Linear(384, 2) 头。
    适配轨以 sample_weight×loss 加权 (与原型系 --adapt-weight 同口径)。
    返回 state_dict 与训练过程信息."""
    torch.manual_seed(seed)
    X, y, sw = _to_xy(emb, labels, extra_emb, extra_labels, extra_weight)
    X = X.to(device); y = y.to(device); sw = sw.to(device)
    head = _Head().to(device)
    opt = torch.optim.Adam(head.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss(reduction="none")
    n = len(X)
    info = {"loss_curve": [], "n_pub": int(len(emb)),
            "n_adapt": int(len(extra_emb) if extra_emb is not None else 0),
            "adapt_weight": float(extra_weight),
            "class_count": {int(c): int((y == c).sum()) for c in (0, 1)}}
    for ep in range(epochs):
        head.train()
        idx = torch.randperm(n, device=device)
        loss_sum, loss_n = 0.0, 0
        for s in range(0, n, bs):
            ii = idx[s:s + bs]
            out = head(X[ii])
            ls = loss_fn(out, y[ii])
            loss = (ls * sw[ii]).sum() / sw[ii].sum().clamp(min=1)
            opt.zero_grad(); loss.backward(); opt.step()
            loss_sum += float(loss.item()) * len(ii)
            loss_n += len(ii)
        info["loss_curve"].append(round(loss_sum / max(loss_n, 1), 4))
    head.eval()
    return head.state_dict(), info


def predict_head(head_state, query_emb, device=DEVICE):
    """单图 embedding -> 预测 0/1。"""
    head = _Head().to(device)
    head.load_state_dict(head_state)
    head.eval()
    with torch.no_grad():
        q = torch.from_numpy(query_emb[None, :]).float().to(device)
        out = head(q)
    return int(out.argmax(1).item())


def save_head(head_state, path):
    torch.save(head_state, path + ".head.pt")


def load_head(path, device=DEVICE):
    head = _Head().to(device)
    head.load_state_dict(torch.load(path + ".head.pt", map_location=device, weights_only=True))
    head.eval()
    return head