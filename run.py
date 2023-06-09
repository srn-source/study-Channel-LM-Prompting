import os
import torch
import numpy as np

from tqdm import tqdm
import torch.nn.functional as F
from model_util import get_optimizer_and_scheduler, get_dataloader
from util import prepro_sentence, prepro_sentence_pair, \
    prepro_sentence_pair_single

def train(logger, model, inputs, batch_size, output_dir,
          learning_rate=1e-5,
          warmup_steps=50,
          num_training_steps=200,
          gradient_accumulation_steps=1,
          max_grad_norm=1.0,
          eval_period=20,
          prompt_tune=False,
          head_tune=False,
          transform_tune=False):
    optimizer, scheduler = get_optimizer_and_scheduler(
        "adamw",
        model,
        learning_rate=learning_rate,
        warmup_steps=warmup_steps,
        num_training_steps=num_training_steps)
    dataloader = get_dataloader(inputs, batch_size, is_training=True)

    n_trainable_params = len([param for param in model.parameters() if param.requires_grad])
    n_gpus = torch.cuda.device_count()
    logger.info("Training {} parameters on {} examples for {} steps using {} GPUs".format(
        n_trainable_params, len(inputs["input_ids"]), num_training_steps, n_gpus))

    model.train()
    global_step = 0
    train_losses = []
    best_accuracy = -1
    stop_training=False

    kkkkk = 0
    logger.info("Start training")
    for epoch in range(num_training_steps):
        for batch in dataloader:
            global_step += 1
            kkkkk = kkkkk + 1
            input_ids=batch[0].cuda()
            attention_mask=batch[1].cuda()
            token_type_ids=batch[2].cuda()
            if len(batch)==3:
                labels=None
            else:
                labels=batch[3].cuda()

            loss = run_model(model, input_ids, attention_mask, token_type_ids, labels=labels)
            loss = loss.mean()

            if torch.isnan(loss).data:
                print ("Stop training because loss=%s" % (loss.data))
                stop_training=True
                break
            train_losses.append(loss.detach().cpu())
            loss.backward()
            if global_step % gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
                optimizer.step()    # We have accumulated enought gradients
                model.zero_grad()
                if scheduler is not None:
                    scheduler.step()

            if global_step % eval_period == 0:
                if prompt_tune:
                    keys = ["transformer.wte.new_embed.weight"]
                    model_state_dict = {key: model.state_dict()[key if n_gpus==1 else "module."+key].cpu() for key in keys}
                elif head_tune:
                    keys = ["lm_head.my_lm_head.weight"]
                    model_state_dict = {key: model.state_dict()[key if n_gpus==1 else "module."+key].cpu() for key in keys}
                elif transform_tune:
                    keys = ["lm_head.transform.weight"]
                    model_state_dict = {key: model.state_dict()[key if n_gpus==1 else "module."+key].cpu() for key in keys}
                else:
                    model_state_dict = {k:v.cpu() for (k, v) in model.state_dict().items()}
                torch.save(model_state_dict,
                           os.path.join(output_dir, "model-{}.pt".format(global_step)))
                logger.info("Saving model at global_step=%d (train loss %.2f)" % \
                            (global_step, np.mean(train_losses)))
                train_losses = []

            if global_step==num_training_steps:
                break

        if global_step==num_training_steps:
            break
    print("kkkkk = " , kkkkk)
    logger.info("Finish training")

def inference(model, inputs, batch_size, tokenizer,return_logits=False):
    #print("inputs inference= ",len(inputs))
    dataloader = get_dataloader(inputs, batch_size, is_training=False)
    #print("dataloader = " , dataloader)
    all_losses = []
    for batch in tqdm(dataloader):
        input_ids=batch[0].cuda()
        attention_mask=batch[1].cuda()
        token_type_ids=batch[2].cuda()
        #print("len(batch) = ", len(batch))
        if len(batch)==3:
            labels=None
        else:
            labels=batch[3].cuda()

        with torch.no_grad():
            loss = run_model(model, input_ids, attention_mask, token_type_ids,tokenizer,
                             labels=labels, return_logits=return_logits)
            #print("loss inference =",loss)
        all_losses += loss.cpu().detach().numpy().tolist()

    return all_losses

def test_remove(model,tokenizer):
    ids1= tokenizer("definitely in the guilty pleasure b-movie category , reign of fire is so incredibly inane that it is laughingly enjoyable")["input_ids"]
    ids2= tokenizer("All in all")["input_ids"]

    bos_token_id = tokenizer.bos_token_id
    eos_token_id = tokenizer.eos_token_id
    
    encoded = prepro_sentence_pair_single(
                    ids1, ids2, 128, bos_token_id, eos_token_id
                )
    input_ids = torch.LongTensor(encoded[0]).cuda()
    attention_mask = torch.LongTensor(encoded[1]).cuda()
    token_type_ids = torch.LongTensor(encoded[2]).cuda()
    
    print(input_ids)
    print(attention_mask)
    print(token_type_ids)
    
    labels = input_ids
    labels = labels[..., 1:].contiguous().unsqueeze(dim=0)
    label_mask = token_type_ids[..., 1:].contiguous().unsqueeze(dim=0)
    
    print("labels0 ===> ",labels.shape)
    print("label_mask0 ===> ",label_mask.shape)
    
    
    outputs = model(input_ids=input_ids, attention_mask=attention_mask)
    logits = outputs.logits[..., :-1, :].contiguous()
    print(logits.shape)
    logits = logits.unsqueeze(dim=0)
    print(logits.shape)
    
    loss_fct = torch.nn.CrossEntropyLoss(reduction="none")
    losses = loss_fct(logits.view(-1, logits.size(-1)),
                      labels.view(-1)) # [batch_size, length]
    losses = losses.unsqueeze(dim=0)
    print(losses.shape)
    losses = losses.view(logits.size(0), logits.size(1)) * label_mask
    print(losses.shape)
    #losses = torch.sum(losses, axis=1) / torch.sum(label_mask, axis=1)
    return torch.sum(losses, axis=1) / torch.sum(label_mask, axis=1)
    # return dict(input_ids=torch.LongTensor(encoded[0]),
    #                 attention_mask=torch.LongTensor(encoded[1]),
    #                 token_type_ids=torch.LongTensor(encoded[2]))
    
def run_model(model, input_ids, attention_mask, token_type_ids, tokenizer = None,
              labels=None, return_logits=False):
    #print(type(input_ids))
    outputs = model(input_ids=input_ids, attention_mask=attention_mask)
    logits = outputs.logits[..., :-1, :].contiguous()
    #print("logits ===> ",logits.shape)

    
    
    if return_logits:
        softmax = torch.nn.Softmax(dim=-1)
        return -torch.log(softmax(logits))

    if labels is None:
        labels = input_ids
    labels = labels[..., 1:].contiguous()
    label_mask = token_type_ids[..., 1:].contiguous()
    
    # print("labels ===> ",labels.shape)
    # print("label_mask ===> ",label_mask.shape)

    loss_fct = torch.nn.CrossEntropyLoss(reduction="none")

    losses = loss_fct(logits.view(-1, logits.size(-1)),
                      labels.view(-1)) # [batch_size, length]
    losses = losses.view(logits.size(0), logits.size(1)) * label_mask
    
    
    # new_en = test_remove(model,tokenizer)
    # print("new_en ===> ",new_en)
    
    return torch.sum(losses, axis=1) / torch.sum(label_mask, axis=1)

