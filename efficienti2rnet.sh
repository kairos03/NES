python train.py ../../imagenet/ILSVRC/Data/CLS-LOC/ --model efficientnet_b0 --num-gpu 4 -j 64 --lr 0.12 --drop 0.2 --img-size 224 --sched step --epochs 700 --decay-epochs 3 --decay-rate 0.97 --opt rmsproptf --warmup-epochs 5 --warmup-lr 1e-6 --weight-decay 1e-5 --opt-eps .001 --batch-size 512 --log-interval 500 --enable_se --sampling --group_se --up_sampling_ratio 1.0  --model-ema
