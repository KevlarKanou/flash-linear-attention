from typing import Optional, Tuple
from dataclasses import dataclass

import math
import torch
import torch.nn.functional as F
import triton
import triton.language as tl

from fla.ops.utils import chunk_local_cumsum
from fla.ops.utils.op import safe_exp
from fla.utils import input_guard, autocast_custom_fwd, autocast_custom_bwd

BLOCK_K = 64


@triton.heuristics(
    {
        "IS_VARLEN": lambda args: args["cu_seqlens"] is not None,
        "USE_INITIAL_STATE": lambda args: args["h0"] is not None,
        "STORE_FINAL_STATE": lambda args: args["ht"] is not None,
    }
)
@triton.autotune(
    configs=[
        triton.Config({"BK": BLOCK_K}, num_warps=num_warps, num_stages=num_stages)
        for num_warps in [4]
        for num_stages in [2, 3, 4]
    ],
    key=["H", "K", "V"],
)
@triton.jit(do_not_specialize=["T"])
def chunkwise_fwd_kernel(
    q,
    k,
    v,
    g,
    l,
    llut,
    o,
    h0,
    ht,
    offsets,
    new_offsets,
    cu_seqlens,
    T,
    H: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    L: tl.constexpr,
    BT: tl.constexpr,
    BK: tl.constexpr,
    L_IN: tl.constexpr,
    L_OUT: tl.constexpr,
    MIN_LEVEL: tl.constexpr,
    MAX_LEVEL: tl.constexpr,
    IS_VARLEN: tl.constexpr,
    USE_INITIAL_STATE: tl.constexpr,
    STORE_FINAL_STATE: tl.constexpr,
):
    p_llut = tl.make_block_ptr(llut, (BT, BT), (BT, 1), (0, 0), (BT, BT), (1, 0))
    b_llut = tl.load(p_llut, boundary_check=(0, 1))
    # parallel over sequences and heads
    i_k = tl.program_id(0)
    i_nh = tl.program_id(1)
    i_n, i_h = i_nh // H, i_nh % H

    if IS_VARLEN:
        bos, eos = (
            tl.load(cu_seqlens + i_n).to(tl.int32),
            tl.load(cu_seqlens + i_n + 1).to(tl.int32),
        )
        T = eos - bos
    else:
        bos, eos = i_n * T, i_n * T + T

    o_i = tl.arange(0, BT)

    # For hierarchical masking
    num_intra_levels = (tl.log2(float(BT))).to(tl.int32) + 1
    i_idx = o_i[:, None]  # BT x 1
    j_idx = o_i[None, :]  # 1 x BT

    # This is not great.
    # See issue: https://github.com/triton-lang/triton/discussions/1313
    KV_0_CREATED = MIN_LEVEL <= 1 and MAX_LEVEL >= 0
    KV_1_CREATED = MIN_LEVEL <= 2 and MAX_LEVEL >= 0
    KV_2_CREATED = MIN_LEVEL <= 3 and MAX_LEVEL >= 1
    KV_3_CREATED = MIN_LEVEL <= 4 and MAX_LEVEL >= 2
    KV_4_CREATED = MIN_LEVEL <= 5 and MAX_LEVEL >= 3
    KV_5_CREATED = MIN_LEVEL <= 6 and MAX_LEVEL >= 4
    KV_6_CREATED = MIN_LEVEL <= 7 and MAX_LEVEL >= 5
    KV_7_CREATED = MIN_LEVEL <= 8 and MAX_LEVEL >= 6
    KV_8_CREATED = MIN_LEVEL <= 9 and MAX_LEVEL >= 7
    KV_9_CREATED = MIN_LEVEL <= 10 and MAX_LEVEL >= 8
    KV_10_CREATED = MIN_LEVEL <= 11 and MAX_LEVEL >= 9
    KV_11_CREATED = MIN_LEVEL <= 12 and MAX_LEVEL >= 10

    kv_0 = tl.zeros([BK, V], dtype=tl.float32)
    kv_1 = tl.zeros([BK, V], dtype=tl.float32)
    kv_2 = tl.zeros([BK, V], dtype=tl.float32)
    kv_3 = tl.zeros([BK, V], dtype=tl.float32)
    kv_4 = tl.zeros([BK, V], dtype=tl.float32)
    kv_5 = tl.zeros([BK, V], dtype=tl.float32)
    kv_6 = tl.zeros([BK, V], dtype=tl.float32)
    kv_7 = tl.zeros([BK, V], dtype=tl.float32)
    kv_8 = tl.zeros([BK, V], dtype=tl.float32)
    kv_9 = tl.zeros([BK, V], dtype=tl.float32)
    kv_10 = tl.zeros([BK, V], dtype=tl.float32)
    kv_11 = tl.zeros([BK, V], dtype=tl.float32)

    offset = 0  # total number to cached tokens
    first_chunk_index = 0  # next chunk index to compute
    if USE_INITIAL_STATE:
        offset = tl.load(offsets + i_n)

        first_chunk_index = offset // BT

        if KV_0_CREATED and (first_chunk_index & 1 > 0):
            p_kv_0 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 0) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_0 = tl.load(p_kv_0, boundary_check=(0, 1))
        if KV_1_CREATED and (first_chunk_index & 2 > 0):
            p_kv_1 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 1) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_1 = tl.load(p_kv_1, boundary_check=(0, 1))
        if KV_2_CREATED and (first_chunk_index & 4 > 0):
            p_kv_2 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 2) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_2 = tl.load(p_kv_2, boundary_check=(0, 1))
        if KV_3_CREATED and (first_chunk_index & 8 > 0):
            p_kv_3 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 3) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_3 = tl.load(p_kv_3, boundary_check=(0, 1))
        if KV_4_CREATED and (first_chunk_index & 16 > 0):
            p_kv_4 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 4) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_4 = tl.load(p_kv_4, boundary_check=(0, 1))
        if KV_5_CREATED and (first_chunk_index & 32 > 0):
            p_kv_5 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 5) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_5 = tl.load(p_kv_5, boundary_check=(0, 1))
        if KV_6_CREATED and (first_chunk_index & 64 > 0):
            p_kv_6 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 6) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_6 = tl.load(p_kv_6, boundary_check=(0, 1))
        if KV_7_CREATED and (first_chunk_index & 128 > 0):
            p_kv_7 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 7) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_7 = tl.load(p_kv_7, boundary_check=(0, 1))
        if KV_8_CREATED and (first_chunk_index & 256 > 0):
            p_kv_8 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 8) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_8 = tl.load(p_kv_8, boundary_check=(0, 1))
        if KV_9_CREATED and (first_chunk_index & 512 > 0):
            p_kv_9 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 9) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_9 = tl.load(p_kv_9, boundary_check=(0, 1))
        if KV_10_CREATED and (first_chunk_index & 1024 > 0):
            p_kv_10 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 10) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_10 = tl.load(p_kv_10, boundary_check=(0, 1))
        if KV_11_CREATED and (first_chunk_index & 2048 > 0):
            p_kv_11 = tl.make_block_ptr(
                h0 + ((i_n * L_IN + 11) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            kv_11 = tl.load(p_kv_11, boundary_check=(0, 1))

    NT = tl.cdiv(T, BT)
    output_offset = -1 * (offset % BT)
    for i_t in range(NT):
        b_h_ptrs = l + ((bos + i_t * BT + i_idx) * H + i_h) * L + b_llut
        b_h = tl.load(b_h_ptrs, mask=i_idx >= j_idx)

        p_g = tl.make_block_ptr(g + bos * H + i_h, (T,), (H,), (i_t * BT,), (BT,), (0,))
        p_q = tl.make_block_ptr(
            q + bos * K, (T, K), (K, 1), (i_t * BT, i_k * BK), (BT, BK), (1, 0)
        )
        p_k = tl.make_block_ptr(
            k + bos * K, (K, T), (1, K), (i_k * BK, i_t * BT), (BK, BT), (0, 1)
        )
        p_v = tl.make_block_ptr(
            v + (bos * H + i_h) * V,
            (T, V),
            (H * V, 1),
            (i_t * BT, 0),
            (BT, V),
            (1, 0),
        )
        p_o = tl.make_block_ptr(
            o + ((bos * H + i_h) * (K // BK) + i_k) * V,
            (T, V),
            (H * (K // BK) * V, 1),
            (i_t * BT + output_offset, 0),
            (BT, V),
            (1, 0),
        )

        b_g = tl.load(p_g, boundary_check=(0,))
        b_q = tl.load(p_q, boundary_check=(0, 1))
        b_k = tl.load(p_k, boundary_check=(0, 1))

        b_s = (tl.dot(b_q, b_k) * safe_exp(b_g[:, None] - b_g[None, :])).to(
            b_q.dtype
        ) * b_h

        b_v = tl.load(p_v, boundary_check=(0, 1))
        b_o = tl.zeros((BT, V), dtype=tl.float32)
        if MIN_LEVEL == 0:
            b_o += tl.dot(b_s, b_v)

        chunk_index = (
            first_chunk_index + i_t
        )  # index of the chunk over the entire sequence, including the offset

        if MIN_LEVEL <= 0 and MAX_LEVEL >= 0:
            if chunk_index & 1:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_0.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 1 and MAX_LEVEL >= 1:
            if chunk_index & 2:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 1),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_1.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 2 and MAX_LEVEL >= 2:
            if chunk_index & 4:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 2),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_2.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 3 and MAX_LEVEL >= 3:
            if chunk_index & 8:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 3),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_3.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 4 and MAX_LEVEL >= 4:
            if chunk_index & 16:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 4),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_4.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 5 and MAX_LEVEL >= 5:
            if chunk_index & 32:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 5),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_5.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 6 and MAX_LEVEL >= 6:
            if chunk_index & 64:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 6),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_6.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 7 and MAX_LEVEL >= 7:
            if chunk_index & 128:  # 8192 - 16384
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 7),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_7.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 8 and MAX_LEVEL >= 8:
            if chunk_index & 256:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 8),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_8.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 9 and MAX_LEVEL >= 9:
            if chunk_index & 512:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 9),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_9.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 10 and MAX_LEVEL >= 10:
            if chunk_index & 1024:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 10),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_10.to(b_q.dtype)) * tl.exp(b_g)[:, None]
        if MIN_LEVEL <= 11 and MAX_LEVEL >= 11:
            if chunk_index & 2048:
                p_l = tl.make_block_ptr(
                    l + (bos * H + i_h) * L,
                    (T, L),
                    (H * L, 1),
                    (i_t * BT, num_intra_levels + 11),
                    (BT, 1),
                    (1, 0),
                )
                b_l = tl.load(p_l, boundary_check=(0, 1))
                b_o += tl.dot((b_l * b_q), kv_11.to(b_q.dtype)) * tl.exp(b_g)[:, None]

        tl.store(p_o, b_o.to(p_o.dtype.element_ty), boundary_check=(0, 1))

        if i_t < NT - 1 or T % BT == 0:
            # Only apply the state update if the last chunk is a full chunk.
            # Otherwise, it needs to be included in the next kernel call.

            # update the recurrent states
            last_idx = min((i_t + 1) * BT, T) - 1
            b_g_last = tl.load(g + bos * H + last_idx * H + i_h)
            if KV_0_CREATED:
                kv_0 *= tl.exp(b_g_last)
            if KV_1_CREATED:
                kv_1 *= tl.exp(b_g_last)
            if KV_2_CREATED:
                kv_2 *= tl.exp(b_g_last)
            if KV_3_CREATED:
                kv_3 *= tl.exp(b_g_last)
            if KV_4_CREATED:
                kv_4 *= tl.exp(b_g_last)
            if KV_5_CREATED:
                kv_5 *= tl.exp(b_g_last)
            if KV_6_CREATED:
                kv_6 *= tl.exp(b_g_last)
            if KV_7_CREATED:
                kv_7 *= tl.exp(b_g_last)
            if KV_8_CREATED:
                kv_8 *= tl.exp(b_g_last)
            if KV_9_CREATED:
                kv_9 *= tl.exp(b_g_last)
            if KV_10_CREATED:
                kv_10 *= tl.exp(b_g_last)
            if KV_11_CREATED:
                kv_11 *= tl.exp(b_g_last)

            b_v = (b_v * tl.exp(b_g_last - b_g)[:, None]).to(b_v.dtype)
            if MIN_LEVEL <= 1:
                kv_0 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 2:
                kv_1 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 3:
                kv_2 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 4:
                kv_3 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 5:
                kv_4 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 6:
                kv_5 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 7:
                kv_6 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 8:
                kv_7 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 9:
                kv_8 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 10:
                kv_9 += tl.dot(b_k, b_v)
            elif MIN_LEVEL == 11:
                kv_10 += tl.dot(b_k, b_v)

            check_value = (~chunk_index & (chunk_index + 1)) - 1

            if MIN_LEVEL <= 1 and MAX_LEVEL >= 0:
                if check_value & 1:
                    kv_1 += kv_0
                    kv_0 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 2 and MAX_LEVEL >= 1:
                if check_value & 2:
                    kv_2 += kv_1
                    kv_1 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 3 and MAX_LEVEL >= 2:
                if check_value & 4:
                    kv_3 += kv_2
                    kv_2 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 4 and MAX_LEVEL >= 3:
                if check_value & 8:
                    kv_4 += kv_3
                    kv_3 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 5 and MAX_LEVEL >= 4:
                if check_value & 16:
                    kv_5 += kv_4
                    kv_4 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 6 and MAX_LEVEL >= 5:
                if check_value & 32:
                    kv_6 += kv_5
                    kv_5 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 7 and MAX_LEVEL >= 6:
                if check_value & 64:
                    kv_7 += kv_6
                    kv_6 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 8 and MAX_LEVEL >= 7:
                if check_value & 128:
                    kv_8 += kv_7
                    kv_7 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 9 and MAX_LEVEL >= 8:
                if check_value & 256:
                    kv_9 += kv_8
                    kv_8 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 10 and MAX_LEVEL >= 9:
                if check_value & 512:
                    kv_10 += kv_9
                    kv_9 = tl.zeros([BK, V], dtype=tl.float32)
            if MIN_LEVEL <= 11 and MAX_LEVEL >= 10:
                if check_value & 1024:
                    kv_11 += kv_10
                    kv_10 = tl.zeros([BK, V], dtype=tl.float32)

    chunk_index = offset // BT + T // BT

    if STORE_FINAL_STATE:
        if (MIN_LEVEL <= 0 and MAX_LEVEL >= 0) and (chunk_index & 1 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 0) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_0, boundary_check=(0, 1))
        if (MIN_LEVEL <= 1 and MAX_LEVEL >= 1) and (chunk_index & 2 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 1) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_1, boundary_check=(0, 1))
        if (MIN_LEVEL <= 2 and MAX_LEVEL >= 2) and (chunk_index & 4 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 2) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_2, boundary_check=(0, 1))
        if (MIN_LEVEL <= 3 and MAX_LEVEL >= 3) and (chunk_index & 8 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 3) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_3, boundary_check=(0, 1))
        if (MIN_LEVEL <= 4 and MAX_LEVEL >= 4) and (chunk_index & 16 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 4) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_4, boundary_check=(0, 1))
        if (MIN_LEVEL <= 5 and MAX_LEVEL >= 5) and (chunk_index & 32 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 5) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_5, boundary_check=(0, 1))
        if (MIN_LEVEL <= 6 and MAX_LEVEL >= 6) and (chunk_index & 64 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 6) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_6, boundary_check=(0, 1))
        if (MIN_LEVEL <= 7 and MAX_LEVEL >= 7) and (chunk_index & 128 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 7) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_7, boundary_check=(0, 1))
        if (MIN_LEVEL <= 8 and MAX_LEVEL >= 8) and (chunk_index & 256 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 8) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_8, boundary_check=(0, 1))
        if (MIN_LEVEL <= 9 and MAX_LEVEL >= 9) and (chunk_index & 512 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 9) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_9, boundary_check=(0, 1))
        if (MIN_LEVEL <= 10 and MAX_LEVEL >= 10) and (chunk_index & 1024 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 10) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_10, boundary_check=(0, 1))
        if (MIN_LEVEL <= 11 and MAX_LEVEL >= 11) and (chunk_index & 2048 > 0):
            p_kv = tl.make_block_ptr(
                ht + ((i_n * L_OUT + 11) * H + i_h) * K * V,
                (K, V),
                (V, 1),
                (i_k * BK, 0),
                (BK, V),
                (1, 0),
            )
            tl.store(p_kv, kv_11, boundary_check=(0, 1))

        tl.store(new_offsets + i_n, (offset // BT) * BT + T)


@triton.heuristics(
    {
        "IS_VARLEN": lambda args: args["cu_seqlens"] is not None,
    }
)
@triton.jit(do_not_specialize=["T"])
def copy_input_kernel(
    q,
    k,
    v,
    g,
    l,
    cu_seqlens,
    q_prev,
    k_prev,
    v_prev,
    g_prev,
    l_prev,
    offsets,
    q_new,
    k_new,
    v_new,
    g_new,
    l_new,
    T,
    H: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    L: tl.constexpr,
    BT: tl.constexpr,
    IS_VARLEN: tl.constexpr,
):
    # parallel over sequences and heads
    i_nh = tl.program_id(0)
    i_n, i_h = i_nh // H, i_nh % H

    if IS_VARLEN:
        bos, eos = (
            tl.load(cu_seqlens + i_n).to(tl.int32),
            tl.load(cu_seqlens + i_n + 1).to(tl.int32),
        )
        T = eos - bos
    else:
        bos, eos = i_n * T, i_n * T + T

    offset = tl.load(offsets + i_n)
    input_offset = -1 * (offset % BT)

    NT = tl.cdiv(T, BT)

    for i_t in range(NT):
        p_g = tl.make_block_ptr(
            g + bos * H + i_h, (T,), (H,), (i_t * BT + input_offset,), (BT,), (0,)
        )
        p_q = tl.make_block_ptr(
            q + bos * K, (T, K), (K, 1), (i_t * BT + input_offset, 0), (BT, K), (1, 0)
        )
        p_k = tl.make_block_ptr(
            k + bos * K, (T, K), (K, 1), (i_t * BT + input_offset, 0), (BT, K), (1, 0)
        )
        p_v = tl.make_block_ptr(
            v + (bos * H + i_h) * V,
            (T, V),
            (H * V, 1),
            (i_t * BT + input_offset, 0),
            (BT, V),
            (1, 0),
        )
        p_g_new = tl.make_block_ptr(
            g_new + bos * H + i_h, (T,), (H,), (i_t * BT,), (BT,), (0,)
        )
        p_q_new = tl.make_block_ptr(
            q_new + bos * K, (T, K), (K, 1), (i_t * BT, 0), (BT, K), (1, 0)
        )
        p_k_new = tl.make_block_ptr(
            k_new + bos * K, (T, K), (K, 1), (i_t * BT, 0), (BT, K), (1, 0)
        )
        p_v_new = tl.make_block_ptr(
            v_new + (bos * H + i_h) * V,
            (T, V),
            (H * V, 1),
            (i_t * BT, 0),
            (BT, V),
            (1, 0),
        )

        b_g = tl.load(p_g, boundary_check=(0,))
        b_q = tl.load(p_q, boundary_check=(0, 1))
        b_k = tl.load(p_k, boundary_check=(0, 1))
        b_v = tl.load(p_v, boundary_check=(0, 1))

        if i_t == 0:
            p_g_prev = tl.make_block_ptr(
                g_prev + i_n * BT * H + i_h, (BT,), (H,), (0,), (BT,), (0,)
            )
            p_q_prev = tl.make_block_ptr(
                q_prev + i_n * BT * K, (BT, K), (K, 1), (0, 0), (BT, K), (1, 0)
            )
            p_k_prev = tl.make_block_ptr(
                k_prev + i_n * BT * K, (BT, K), (K, 1), (0, 0), (BT, K), (1, 0)
            )
            p_v_prev = tl.make_block_ptr(
                v_prev + (i_n * BT * H + i_h) * V,
                (BT, V),
                (H * V, 1),
                (0, 0),
                (BT, V),
                (1, 0),
            )

            b_g += tl.load(p_g_prev, boundary_check=(0,))
            b_q += tl.load(p_q_prev, boundary_check=(0, 1))
            b_k += tl.load(p_k_prev, boundary_check=(0, 1))
            b_v += tl.load(p_v_prev, boundary_check=(0, 1))

        tl.store(p_g_new, b_g, boundary_check=(0,))
        tl.store(p_q_new, b_q, boundary_check=(0, 1))
        tl.store(p_k_new, b_k, boundary_check=(0, 1))
        tl.store(p_v_new, b_v, boundary_check=(0, 1))

        for i in range(L):
            p_l = tl.make_block_ptr(
                l + (bos * H + i_h) * L,
                (T, L),
                (H * L, 1),
                (i_t * BT + input_offset, i),
                (BT, 1),
                (1, 0),
            )
            p_l_new = tl.make_block_ptr(
                l_new + (bos * H + i_h) * L,
                (T, L),
                (H * L, 1),
                (i_t * BT, i),
                (BT, 1),
                (1, 0),
            )
            b_l = tl.load(p_l, boundary_check=(0,))
            if i_t == 0:
                p_l_prev = tl.make_block_ptr(
                    l_prev + (i_n * BT * H + i_h) * L,
                    (BT, L),
                    (H * L, 1),
                    (0, i),
                    (BT, 1),
                    (1, 0),
                )
                b_l += tl.load(p_l_prev, boundary_check=(0,))
            tl.store(p_l_new, b_l, boundary_check=(0,))


@triton.heuristics(
    {
        "IS_VARLEN": lambda args: args["cu_seqlens"] is not None,
    }
)
@triton.jit(do_not_specialize=["T"])
def copy_last_chunk_kernel(
    q,
    k,
    v,
    g,
    l,
    cu_seqlens,
    q_prev,
    k_prev,
    v_prev,
    g_prev,
    l_prev,
    offsets,
    T,
    H: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    L: tl.constexpr,
    BT: tl.constexpr,
    IS_VARLEN: tl.constexpr,
):
    # parallel over sequences and heads
    i_nh = tl.program_id(0)
    i_n, i_h = i_nh // H, i_nh % H

    if IS_VARLEN:
        bos, eos = (
            tl.load(cu_seqlens + i_n).to(tl.int32),
            tl.load(cu_seqlens + i_n + 1).to(tl.int32),
        )
        T = eos - bos
    else:
        bos, eos = i_n * T, i_n * T + T

    seq_offset = (T // BT) * BT

    p_g = tl.make_block_ptr(g + bos * H + i_h, (T,), (H,), (seq_offset,), (BT,), (0,))
    p_q = tl.make_block_ptr(
        q + bos * K, (T, K), (K, 1), (seq_offset, 0), (BT, K), (1, 0)
    )
    p_k = tl.make_block_ptr(
        k + bos * K, (T, K), (K, 1), (seq_offset, 0), (BT, K), (1, 0)
    )
    p_v = tl.make_block_ptr(
        v + (bos * H + i_h) * V,
        (T, V),
        (H * V, 1),
        (seq_offset, 0),
        (BT, V),
        (1, 0),
    )
    p_g_prev = tl.make_block_ptr(
        g_prev + i_n * BT * H + i_h, (BT,), (H,), (0,), (BT,), (0,)
    )
    p_q_prev = tl.make_block_ptr(
        q_prev + i_n * BT * K, (BT, K), (K, 1), (0, 0), (BT, K), (1, 0)
    )
    p_k_prev = tl.make_block_ptr(
        k_prev + i_n * BT * K, (BT, K), (K, 1), (0, 0), (BT, K), (1, 0)
    )
    p_v_prev = tl.make_block_ptr(
        v_prev + (i_n * BT * H + i_h) * V, (BT, V), (H * V, 1), (0, 0), (BT, V), (1, 0)
    )

    tl.store(p_g_prev, tl.load(p_g, boundary_check=(0,)), boundary_check=(0,))
    tl.store(p_q_prev, tl.load(p_q, boundary_check=(0, 1)), boundary_check=(0, 1))
    tl.store(p_k_prev, tl.load(p_k, boundary_check=(0, 1)), boundary_check=(0, 1))
    tl.store(p_v_prev, tl.load(p_v, boundary_check=(0, 1)), boundary_check=(0, 1))

    for i in range(L):
        p_l = tl.make_block_ptr(
            l + (bos * H + i_h) * L,
            (T, L),
            (H * L, 1),
            (seq_offset, i),
            (BT, 1),
            (1, 0),
        )
        p_l_prev = tl.make_block_ptr(
            l_prev + (i_n * BT * H + i_h) * L,
            (BT, L),
            (H * L, 1),
            (0, i),
            (BT, 1),
            (1, 0),
        )
        tl.store(p_l_prev, tl.load(p_l, boundary_check=(0,)), boundary_check=(0,))


def construct_binary_level_mask(level, T):
    if level == 0:
        return torch.diag(torch.ones(T, dtype=torch.bool))

    indices = torch.cartesian_prod(torch.arange(T), torch.arange(T))

    mask = torch.where(
        torch.logical_and(
            torch.logical_and(
                indices[:, 0] % (1 << level) >= (1 << (level - 1)),
                indices[:, 1] + (1 << (level - 1))
                >= indices[:, 0] - (indices[:, 0] % (1 << (level - 1))),
            ),
            indices[:, 1] < indices[:, 0] - (indices[:, 0] % (1 << (level - 1))),
        ).view(T, T),
        1,
        0,
    )

    return mask


def level_lut(BT, device):
    lut = torch.zeros((BT, BT), dtype=torch.int32, device=device)
    for level in range(1, ceil_log(BT, 2) + 1):
        mask = construct_binary_level_mask(level, BT).to(device)
        lut = torch.where(mask.to(torch.bool), level, lut)
    return lut


def ceil_div(x: int, y: int) -> int:
    return math.ceil(x / y)


def ceil_log(x: int, b: int) -> int:
    return math.ceil(math.log(x, b))


@dataclass
class LogLinearAttentionState:
    ht: torch.Tensor
    offsets: torch.Tensor
    q_prev: torch.Tensor
    k_prev: torch.Tensor
    v_prev: torch.Tensor
    g_prev: torch.Tensor
    l_prev: torch.Tensor


class ChunkLogLinearAttentionFunction(torch.autograd.Function):
    @staticmethod
    @input_guard
    @autocast_custom_fwd
    def forward(
        ctx,
        q,
        k,
        v,
        g,
        l,
        initial_state,
        output_final_state,
        cu_seqlens,
    ):
        B, T, G, K = k.shape
        _, _, H, V = v.shape
        _, _, _, L = l.shape

        if G != 1:
            raise ValueError("Group dimension must be 1.")

        BT = 64  # chunk size

        h0 = initial_state.ht if initial_state is not None else None
        offsets = initial_state.offsets if initial_state is not None else None

        if cu_seqlens is None:
            NT = ceil_div(T + (torch.max(offsets) if offsets is not None else 0), BT)
            MAX_LEVEL = ceil_log(NT, 2) - 1
        else:
            NT = max(
                [
                    ceil_div(
                        cu_seqlens[i + 1]
                        - cu_seqlens[i]
                        + (offsets[i] if offsets is not None else 0),
                        BT,
                    )
                    for i in range(len(cu_seqlens) - 1)
                ]
            )
            MAX_LEVEL = ceil_log(NT, 2) - 1
            B = len(cu_seqlens) - 1

        if MAX_LEVEL > 10:
            raise ValueError("Sequence length must be less than 2**17")

        S0 = B if cu_seqlens is None else 1
        o = torch.zeros(
            (S0, T, H, (K // BLOCK_K), V),
            dtype=v.dtype,
            device=v.device,
        )

        if initial_state is not None:
            if cu_seqlens is not None:
                cu_seqlens = cu_seqlens + F.pad(torch.cumsum(offsets % BT), (1, 0))
            else:
                assert (offsets == offsets[0]).all()
                T += offsets[0].item() % BT
            S1 = cu_seqlens[-1] if cu_seqlens is not None else T
            q_new = torch.zeros((S0, S1, G, K), dtype=q.dtype, device=q.device)
            k_new = torch.zeros((S0, S1, G, K), dtype=k.dtype, device=k.device)
            v_new = torch.zeros((S0, S1, H, V), dtype=v.dtype, device=v.device)
            g_new = torch.zeros((S0, S1, H), dtype=g.dtype, device=g.device)
            l_new = torch.zeros((S0, S1, H, L), dtype=l.dtype, device=l.device)

            copy_input_kernel[(B * H,)](
                q=q,
                k=k,
                v=v,
                g=g,
                l=l,
                cu_seqlens=cu_seqlens,
                q_prev=initial_state.q_prev,
                k_prev=initial_state.k_prev,
                v_prev=initial_state.v_prev,
                g_prev=initial_state.g_prev,
                l_prev=initial_state.l_prev,
                q_new=q_new,
                k_new=k_new,
                v_new=v_new,
                g_new=g_new,
                l_new=l_new,
                offsets=offsets,
                T=T,
                H=H,
                K=K,
                V=V,
                L=L,
                BT=BT,
            )
            q = q_new
            k = k_new
            v = v_new
            g = g_new
            l = l_new

        # Store one extra level (MAX_LEVEL + 2) in case the length is multiple of 2
        ht = (
            torch.zeros((B, MAX_LEVEL + 2, H, K, V), dtype=torch.float, device=v.device)
            if output_final_state
            else None
        )

        new_offsets = torch.zeros((B,), dtype=torch.int32, device=v.device)
        g = chunk_local_cumsum(
            g, BT, offsets=None, head_first=None, cu_seqlens=cu_seqlens
        )

        def grid(meta):
            return (triton.cdiv(K, meta["BK"]), B * H)

        l_in = h0.shape[1] if initial_state is not None else None
        l_out = ht.shape[1] if output_final_state else None

        chunkwise_fwd_kernel[grid](
            q=q,
            k=k,
            v=v,
            g=g,
            l=l,
            llut=level_lut(BT, v.device),
            o=o,
            h0=h0,
            ht=ht,
            offsets=offsets,
            new_offsets=new_offsets,
            cu_seqlens=cu_seqlens,
            T=T,
            H=H,
            K=K,
            V=V,
            L=L,
            BT=BT,
            L_IN=l_in,
            L_OUT=l_out,
            MIN_LEVEL=0,
            MAX_LEVEL=MAX_LEVEL,
        )

        if output_final_state:
            q_prev = torch.zeros((B, BT, G, K), dtype=q.dtype, device=q.device)
            k_prev = torch.zeros((B, BT, G, K), dtype=k.dtype, device=k.device)
            v_prev = torch.zeros((B, BT, H, V), dtype=v.dtype, device=v.device)
            g_prev = torch.zeros((B, BT, H), dtype=g.dtype, device=g.device)
            l_prev = torch.zeros((B, BT, H, L), dtype=l.dtype, device=l.device)

            copy_last_chunk_kernel[(B * H,)](
                q=q,
                k=k,
                v=v,
                g=g,
                l=l,
                cu_seqlens=cu_seqlens,
                q_prev=q_prev,
                k_prev=k_prev,
                v_prev=v_prev,
                g_prev=g_prev,
                l_prev=l_prev,
                offsets=new_offsets,
                T=T,
                H=H,
                K=K,
                V=V,
                L=L,
                BT=BT,
            )

            final_state = LogLinearAttentionState(
                ht=ht,
                offsets=new_offsets,
                q_prev=q_prev,
                k_prev=k_prev,
                v_prev=v_prev,
                g_prev=g_prev,
                l_prev=l_prev,
            )
            return o.sum(dim=-2), final_state

        return o.sum(dim=-2), None

    @staticmethod
    @input_guard
    @autocast_custom_bwd
    def backward(ctx, do, dht):
        raise NotImplementedError(
            "Backward pass is not implemented for log-linear attention."
        )


@torch.compiler.disable
def chunk_log_linear_attn(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    l: torch.Tensor,
    initial_state: Optional[torch.Tensor] = None,
    output_final_state: bool = False,
    cu_seqlens: Optional[torch.LongTensor] = None,
) -> Tuple[torch.Tensor, torch.Tensor]:
    r"""
    Args:
        q (torch.Tensor):
            queries of shape `[B, T, H, K]`.
        k (torch.Tensor):
            keys of shape `[B, T, H, K]`.
        v (torch.Tensor):
            values of shape `[B, T, H, V]`.
        g (torch.Tensor):
            Forget gates of shape `[B, T, H]`.
        l (torch.Tensor):
            Scales for each level of shape `[B, T, H, L]`.
        initial_state (Optional[torch.Tensor]):
            Initial state of shape `[N, H, K, V]` for `N` input sequences.
            For equal-length input sequences, `N` equals the batch size `B`.
            Default: `None`.
        output_final_state (Optional[bool]):
            Whether to output the final state of shape `[N, H, K, V]`. Default: `False`.
        cu_seqlens (torch.LongTensor):
            Cumulative sequence lengths of shape `[N+1]` used for variable-length training,
            consistent with the FlashAttention API.

    Returns:
        o (torch.Tensor):
            Outputs of shape `[B, T, H, V]`.
        final_state (torch.Tensor):
            Final state of type `LogLinearAttentionState` if `output_final_state=True` else `None`.

    """
    if cu_seqlens is not None:
        if q.shape[0] != 1:
            raise ValueError(
                f"The batch size is expected to be 1 rather than {q.shape[0]} when using `cu_seqlens`."
                f"Please flatten variable-length inputs before processing."
            )

    o, final_state = ChunkLogLinearAttentionFunction.apply(
        q,
        k,
        v,
        g,
        l,
        initial_state,
        output_final_state,
        cu_seqlens,
    )
    return o, final_state
