"""
# Copyright (c) 2022 Microsoft
# Licensed under The MIT License [see LICENSE for details]

import math

import torch
import torch.nn.functional as F
from torch import nn
try:
    from apex.normalization import FusedLayerNorm as LayerNorm
except ModuleNotFoundError:
    from torch.nn import LayerNorm

from .multiway_network import MultiwayWrapper
from .xpos_relative_position import XPOS
from einops import rearrange

class MultiheadAttention(nn.Module):
    def __init__(
        self,
        args,
        embed_dim,
        num_heads,
        dropout=0.0,
        self_attention=False,
        encoder_decoder_attention=False,
        subln=False,
        casual=False
    ):
        super().__init__()
        self.args = args
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads
        self.scaling = self.head_dim**-0.5

        self.self_attention = self_attention
        self.encoder_decoder_attention = encoder_decoder_attention
        assert self.self_attention ^ self.encoder_decoder_attention

        self.k_proj = MultiwayWrapper(args, nn.Linear(embed_dim, embed_dim, bias=True))
        self.v_proj = MultiwayWrapper(args, nn.Linear(embed_dim, embed_dim, bias=True))
        self.q_proj = MultiwayWrapper(args, nn.Linear(embed_dim, embed_dim, bias=True))
        self.out_proj = MultiwayWrapper(
            args, nn.Linear(embed_dim, embed_dim, bias=True)
        )
        self.inner_attn_ln = (
            MultiwayWrapper(args, LayerNorm(self.embed_dim, eps=args.layernorm_eps))
            if subln and self.self_attention
            else None
        )
        self.dropout_module = torch.nn.Dropout(dropout)
        self.xpos = (
            XPOS(self.head_dim, args.xpos_scale_base)
            if args.xpos_rel_pos and self.self_attention
            else None
        )
        self.casual = casual
        self.flash_config = args.flash_config

    def flash_scaled_dot_product_attention(self, q, k, v, attn_mask=None):
        _, heads, q_len, _, k_len, is_cuda = *q.shape, k.shape[-2], q.is_cuda()

        #check if masks exists and expand to compatible shape
        if attn_mask is not None and attn_mask.ndim != 4:
            mask = rearrange(attn_mask, 'b j -> b 1 1 j')
            mask = mask.expand(-1, heads, q_len, -1)

        #check if threis a comptaible device for flash attention
        config = self.flash_config if is_cuda else self.cpu_config

        with torch.backends.cuda.sdp_kernel(**config._asdict()):
            attn_weights = torch.bmm(q, k.tranpose(1, 2))
            attn_weights = F.softmax(attn_weights, dim=-1, dtype=torch.float32).type_as(attn_weights)

            if attn_mask is not None:
                attn_weights += attn_mask
            
            attn_probs = self.dropout_module(attn_weights)
            attn = torch.bmm(attn_probs, v)

        return attn, attn_weights
    

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.k_proj.weight, gain=1 / math.sqrt(2))
        nn.init.xavier_uniform_(self.v_proj.weight, gain=1 / math.sqrt(2))
        nn.init.xavier_uniform_(self.q_proj.weight, gain=1 / math.sqrt(2))
        nn.init.xavier_uniform_(self.out_proj.weight)
        nn.init.constant_(self.out_proj.bias, 0.0)

    def forward(
        self,
        query,
        key,
        value,
        incremental_state=None,
        key_padding_mask=None,
        attn_mask=None,
        rel_pos=None,
    ):
        bsz, tgt_len, embed_dim = query.size()
        src_len = tgt_len
        assert embed_dim == self.embed_dim, f"query dim {embed_dim} != {self.embed_dim}"

        key_bsz, src_len, _ = key.size()
        assert key_bsz == bsz, f"{query.size(), key.size()}"
        assert value is not None
        assert bsz, src_len == value.shape[:2]

        q = self.q_proj(query)
        k = self.k_proj(key)
        v = self.v_proj(value)
        q *= self.scaling

        q = q.view(bsz, tgt_len, self.num_heads, self.head_dim).transpose(1, 2)
        k = k.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)
        v = v.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)
        q = q.reshape(bsz * self.num_heads, tgt_len, self.head_dim)
        k = k.reshape(bsz * self.num_heads, src_len, self.head_dim)
        v = v.reshape(bsz * self.num_heads, src_len, self.head_dim)

        if incremental_state is not None:
            if "prev_key" in incremental_state:
                prev_key = incremental_state["prev_key"].view(
                    bsz * self.num_heads, -1, self.head_dim
                )
                prev_value = incremental_state["prev_value"].view(
                    bsz * self.num_heads, -1, self.head_dim
                )
                k = torch.cat([prev_key, k], dim=1)
                v = torch.cat([prev_value, v], dim=1)
            incremental_state["prev_key"] = k.view(
                bsz, self.num_heads, -1, self.head_dim
            )
            incremental_state["prev_value"] = v.view(
                bsz, self.num_heads, -1, self.head_dim
            )
            src_len = k.size(1)

        if self.xpos is not None:
            if incremental_state is not None:
                offset = src_len - 1
            else:
                offset = 0
            k = self.xpos(k, offset=0, downscale=True)
            q = self.xpos(q, offset=offset, downscale=False)

        attn_weights = torch.bmm(q, k.transpose(1, 2))

        # if attn_mask is not None:
        #     attn_weights = torch.nan_to_num(attn_weights)
        #     attn_mask = attn_mask.unsqueeze(0)
        #     attn_weights += attn_mask

        # if key_padding_mask is not None:
        #     attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
        #     attn_weights = attn_weights.masked_fill(
        #         key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool),
        #         float("-inf"),
        #     )
        #     attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)

        attn, attn_weights = self.flash_scaled_dot_product_attention(q, k, v, attn_mask)

        if rel_pos is not None:
            rel_pos = rel_pos.view(attn_weights.size())
            attn_weights = attn_weights + rel_pos

        attn_weights = F.softmax(attn_weights, dim=-1, dtype=torch.float32).type_as(
            attn_weights
        )
        
        attn_probs = self.dropout_module(attn_weights)

        attn = torch.bmm(attn_probs, v)

        # attn_weights = F.softmax(attn_weights, dim=-1, dtype=torch.float32).type_as(
        #     attn_weights
        # )
        # attn_probs = self.dropout_module(attn_weights)

        # attn = torch.bmm(attn_probs, v)
        attn = attn.transpose(0, 1).reshape(tgt_len, bsz, embed_dim).transpose(0, 1)

        if self.inner_attn_ln is not None:
            attn = self.inner_attn_ln(attn)

        attn = self.out_proj(attn)
        attn_weights = attn_weights.view(
            bsz, self.num_heads, tgt_len, src_len
        ).transpose(1, 0)

        return attn, attn_weights


"""





#V2 ===================================>
import math 
import torch 
import torch.nn.functional as F
from torch import nn
try:
    from apex.normalization import FusedLayerNorm as LayerNorm
except ModuleNotFoundError:
    from torch.nn import LayerNorm

from multiway_network import MultiwayWrapper
from xpos_relative_position import XPOS
from einops import rearrange
# from flash_attention import FlashAttention, FlashMHA
from flash_attn.flash_attention import FlashMHA

#sparsificaiton, pruning, fp16, layer norm, keys and values are precomputed for the encoder decoder mechanism
class MultiheadAttention(nn.Module):
    def __init__(
            self,
            args,
            embed_dim,
            num_heads,
            dropout=0.0,
            self_attention=False,
            encoder_decoder_attention=False,
            subln=False,
            casual=False,
            flash_attention=False
    ):
        super().__init__()
        self.args = args
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads
        self.scaling = self.head_dim**-0.5

        self.self_attention = self_attention
        self.encoder_decoder_attention = encoder_decoder_attention
        assert self.self_attention ^ self.encoder_decoder_attention

        self.k_proj = MultiwayWrapper(args, nn.Linear(embed_dim, embed_dim, bias=True ))
        self.v_proj = MultiwayWrapper(args, nn.Linear(embed_dim, embed_dim, bias=True))
        self.q_proj = MultiwayWrapper(args, nn.Linear(embed_dim, embed_dim, bias=True))
        self.out_proj = MultiwayWrapper(
            args, nn.Linear(embed_dim, embed_dim, bias=True)
        )
        self.inner_attn_ln = (
            MultiwayWrapper(args, LayerNorm(self.embed_dim, eps=args.layernorm_eps))
            if subln and self.self_attention
            else None
        )
        self.dropout_module = torch.nn.Dropout(dropout)
        self.xpos = (
            XPOS(self.head_dim, args.xpos_scale_base)
            if args.xpos_rel_pos and self.self_attention
            else None
        )
        self.casual = casual
        self.flash_attention = flash_attention
        if flash_attention:
            self.flash_mha = FlashMHA(embed_dim, num_heads, attention_dropout=dropout, causal=casual)
        self.flash_config = args.flash_config

    def apply_pruning(self, tensor, top_k=0.5):
        k = max(int(tensor.shape[-1] * top_k), 1)
        _, indices = torch.topk(tensor, k, dim=-1, sorted=False)
        mask = torch.zeros_like(tensor).scatter_(-1, indices, 1.0)
        pruned_tensor = tensor * mask
        return pruned_tensor
    
    def forward(
        self,
        query,
        key,
        value,
        incremental_state=None,
        key_padding_mask=None,
        attn_mask=None,
        rel_pos=None,
        precomputed_kv=False
    ):
        bsz, tgt_len, embed_dim = query.size()
        src_len = tgt_len
        assert embed_dim == self.embed_dim, f"query dim {embed_dim} != {self.embed_dim}"

        key_bsz, src_len, _ = key.size()
        assert key_bsz == bsz, f"{query.size(), key.size()}"
        assert value is not None
        assert bsz, src_len == value.shape[:2]

        if not precomputed_kv:
            k = self.k_proj(key)
            v = self.v_proj(value)


        q = self.q_proj(query)
        k = self.k_proj(key)
        v = self.v_proj(value)
        
        q = q * self.scaling


        #pruning/sparsification
        q = self.apply_pruning(q)
        k = self.apply_pruning(k)
        v = self.apply_pruning(v)


        # flash attention
        if self.flash_attention:
            # Use FlashAttention instead of the default scaled dot product attention
            q = q.view(bsz, tgt_len, self.num_heads, self.head_dim).transpose(1, 2)
            k = k.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)
            v = v.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)

            qkv = torch.stack([q, k, v], dim=2)
            attn_output, attn_output_weights = self.flash_mha(qkv, key_padding_mask=key_padding_mask)
        else:
            q = q.view(bsz, tgt_len, self.num_heads, self.head_dim).transpose(1, 2)
            k = k.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)
            v = v.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)


            q = q.reshape(bsz * self.num_heads, tgt_len, self.head_dim)
            k = k.reshape(bsz * self.num_heads, tgt_len, self.head_dim)
            v = v.reshape(bsz * self.num_heads, tgt_len, self.head_dim)

        if key_padding_mask is not None:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights.masked_fill(
                key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool),
                float("-inf"),
            )
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)



        if incremental_state is not None:
            if "prev_key" in incremental_state:
                prev_key = incremental_state["prev_key"].view(
                    bsz * self.num_heads, -1, self.head_dim
                )
                prev_value = incremental_state["prev_value"].view(
                    bsz * self.num_heads, -1, self.head_dim
                )
                k = torch.cat([prev_key, k], dim=1)
                v = torch.cat([prev_value, v], dim=1)
            incremental_state["prev_key"] = k.view(
                bsz, self.num_heads, -1, self.head_dim
            )
            incremental_state["prev_value"] = v.view(
                bsz, self.num_heads, -1, self.head_dim
            )
            src_len = k.size(1)

        if self.xpos is not None:
            if incremental_state is not None:
                offset = src_len - 1
            else:
                offset = 0
            k = self.xpos(k, offset=0, downscale=True)
            q = self.xpos(q, offset=offset, downscale=False)

        attn_weights = torch.bmm(q, k.transpose(1, 2))

        if attn_mask is not None:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights += attn_mask.unsqueeze(1).expand(-1, self.num_heads, -1, -1)

        if key_padding_mask is not None:
            attn_weights = attn_weights.masked_fill(
                key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool),
                float("-inf"),
            )
        
        attn_weights = F.softmax(attn_weights, dim=-1, dtype=torch.float32).type_as(
            attn_weights
        )

        if rel_pos is not None:
            rel_pos = rel_pos.view(attn_weights.size())
            attn_weights = attn_weights + rel_pos

        #convert attention weights to mixed precision fp16
        attn_weights = attn_weights.to(torch.float16)

        
        attn_probs = self.dropout_module(attn_weights)

        #convert attention weights back to original dtype
        attn_probs = attn_probs.to(torch.float32)


        attn = torch.bmm(attn_probs, v)
        attn = attn.transpose(0, 1).reshape(tgt_len, bsz, embed_dim).transpose(0, 1)

        if self.inner_attn_ln is not None:
            attn = self.inner_attn_ln(attn)

        attn = self.out_proj(attn)

        attn_weights = attn_weights.view(
            bsz, self.num_heads, tgt_len, src_len
        ).transpose(1, 0)

        return attn, attn_weights
    
"""


Yes, the integrated Flash module takes into account dropout, key padding masking, and causal masking. Here's a breakdown of how each of these components is handled in the provided code:

Dropout: The FlashAttention class has a dropout_p attribute, which is passed to the forward function of flash_attn_unpadded_qkvpacked_func. The dropout rate is applied to the attention weights during the forward pass. The dropout rate is used only during training (self.training is True), and no dropout is applied during evaluation.
python
Copy code
output_unpad = flash_attn_unpadded_qkvpacked_func(
    x_unpad, cu_seqlens, max_s, self.dropout_p if self.training else 0.0,
    softmax_scale=self.softmax_scale, causal=causal
)
Key padding masking: When you pass the key_padding_mask to the forward function of the FlashAttention class, it handles the key padding mask by appropriately unpadding the input and re-padding the output:
python
Copy code
x_unpad, indices, cu_seqlens, max_s = unpad_input(x, key_padding_mask)
# ...
output = rearrange(pad_input(rearrange(output_unpad, 'nnz h d -> nnz (h d)'),
                            indices, batch_size, seqlen),
                    'b s (h d) -> b s h d', h=nheads)
Causal masking: The FlashAttention class has a causal attribute that is passed to the forward function of flash_attn_unpadded_qkvpacked_func. This attribute is used to apply causal masking during the attention calculation:
python
Copy code
output_unpad = flash_attn_unpadded_qkvpacked_func(
    x_unpad, cu_seqlens, max_s, self.dropout_p if self.training else 0.0,
    softmax_scale=self.softmax_scale, causal=causal
)
As you can see, the Flash module handles dropout, masking, and causal masking during its forward pass.

"""



"""
forward v2


  
    def forward(
        self,
        query,
        key,
        value,
        incremental_state=None,
        key_padding_mask=None,
        attn_mask=None,
        rel_pos=None,
        precomputed_kv=False
    ):
        bsz, tgt_len, embed_dim = query.size()
        src_len = tgt_len
        assert embed_dim == self.embed_dim, f"query dim {embed_dim} != {self.embed_dim}"

        key_bsz, src_len, _ = key.size()
        assert key_bsz == bsz, f"{query.size(), key.size()}"
        assert value is not None
        assert bsz, src_len == value.shape[:2]

        if not precomputed_kv:
            k = self.k_proj(key)
            v = self.v_proj(value)


        q = self.q_proj(query)
        k = self.k_proj(key)
        v = self.v_proj(value)
        
        q = q * self.scaling


        #pruning/sparsification
        q = self.apply_pruning(q)
        k = self.apply_pruning(k)
        v = self.apply_pruning(v)


        # flash attention
        if self.flash_attention:
            # Use FlashAttention instead of the default scaled dot product attention
            q = q.view(bsz, tgt_len, self.num_heads, self.head_dim).transpose(1, 2)
            k = k.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)
            v = v.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)

            if self.xpos is not None:
                q = self.xpos(q)
                k = self.xpos(k)

            qkv = torch.stack([q, k, v], dim=2)
            attn_output, attn_output_weights = self.flash_mha(qkv, key_padding_mask=key_padding_mask)
        else:
            q = q.view(bsz, tgt_len, self.num_heads, self.head_dim).transpose(1, 2)
            k = k.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)
            v = v.view(bsz, src_len, self.num_heads, self.head_dim).transpose(1, 2)


            q = q.reshape(bsz * self.num_heads, tgt_len, self.head_dim)
            k = k.reshape(bsz * self.num_heads, tgt_len, self.head_dim)
            v = v.reshape(bsz * self.num_heads, tgt_len, self.head_dim)

        if key_padding_mask is not None:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights = attn_weights.masked_fill(
                key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool),
                float("-inf"),
            )
            attn_weights = attn_weights.view(bsz * self.num_heads, tgt_len, src_len)



        if incremental_state is not None:
            if "prev_key" in incremental_state:
                prev_key = incremental_state["prev_key"].view(
                    bsz * self.num_heads, -1, self.head_dim
                )
                prev_value = incremental_state["prev_value"].view(
                    bsz * self.num_heads, -1, self.head_dim
                )
                k = torch.cat([prev_key, k], dim=1)
                v = torch.cat([prev_value, v], dim=1)
            incremental_state["prev_key"] = k.view(
                bsz, self.num_heads, -1, self.head_dim
            )
            incremental_state["prev_value"] = v.view(
                bsz, self.num_heads, -1, self.head_dim
            )
            src_len = k.size(1)

        if self.xpos is not None:
            if incremental_state is not None:
                offset = src_len - 1
            else:
                offset = 0
            k = self.xpos(k, offset=0, downscale=True)
            q = self.xpos(q, offset=offset, downscale=False)

        attn_weights = torch.bmm(q, k.transpose(1, 2))

        if attn_mask is not None:
            attn_weights = attn_weights.view(bsz, self.num_heads, tgt_len, src_len)
            attn_weights += attn_mask.unsqueeze(1).expand(-1, self.num_heads, -1, -1)

        if key_padding_mask is not None:
            attn_weights = attn_weights.masked_fill(
                key_padding_mask.unsqueeze(1).unsqueeze(2).to(torch.bool),
                float("-inf"),
            )
        
        attn_weights = F.softmax(attn_weights, dim=-1, dtype=torch.float32).type_as(
            attn_weights
        )

        if rel_pos is not None:
            rel_pos = rel_pos.view(attn_weights.size())
            attn_weights = attn_weights + rel_pos

        #convert attention weights to mixed precision fp16
        attn_weights = attn_weights.to(torch.float16)

        
        attn_probs = self.dropout_module(attn_weights)

        #convert attention weights back to original dtype
        attn_probs = attn_probs.to(torch.float32)


        attn = torch.bmm(attn_probs, v)
        attn = attn.transpose(0, 1).reshape(tgt_len, bsz, embed_dim).transpose(0, 1)

        if self.inner_attn_ln is not None:
            attn = self.inner_attn_ln(attn)

        attn = self.out_proj(attn)

        attn_weights = attn_weights.view(
            bsz, self.num_heads, tgt_len, src_len
        ).transpose(1, 0)

        return attn, attn_weights

"""