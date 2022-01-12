# MetaKG

Source code for MetaKG: Meta-learning on Knowledge Graph for Cold-start Recommendation

## Environment Requirements

- Ubuntu OS
- Python >= 3.8 (Anaconda3 is recommended)
- PyTorch 1.7+
- A Nvidia GPU with cuda 11.1+ (RTX 3090 is preferred)

## Datasets

We user three popular datasets Amazon-book, Last-FM, and Yelp2018 to conduct experiments.
* We follow the paper "[KGAT: Knowledge Graph Attention Network for Recommendation](https://arxiv.org/abs/1905.07854)" to process data.
* In order to construct cold-start scenario, we find user registration time, item publication time or first interaction time in the full version of recommendation datasets. Then we divide new and old ones in chronological order. 
* For Amazon-book, download book reviews (5-core) from [here](http://jmcauley.ucsd.edu/data/amazon), put it into related `rawdata` folder.
* For Last-FM, download LFM-1b dataset from [here](http://www.cp.jku.at/datasets/LFM-1b/), unzip and put it into related `rawdata` folder.
* For Yelp2018, download Yelp2018 version dataset from [here](https://www.heywhale.com/mw/dataset/5ecbc342fac16e0036ec41a0),  unzip and put it into related `rawdata` folder.

The prepared folder structure is like this:

```
- Datasets
    - pretrain
    - amazon-book
	- rawdata
		- reviews_Books_5.json.gz
    - last-fm
    	- rawdata
    		- LFM-1b_albums.txt
    		- LFM-1b_artists.txt
    		- ...
    - yelp2018
    	- rawdata
    		- yelp_academic_dataset_business.json
    		- yelp_academic_dataset_checkin.json
    		- ...
```

## Train

1. Cold-start scenario constructing (it may take a while)

   ```shell
   python construct_data.py
   ```

2. Start training (the log of learning process can be found in `trained_model` folder)

   ```shell
   python main_MetaKG.py
   ```

## Reference

* We use the codes of [KGAT](https://github.com/xiangwang1223/knowledge_graph_attention_network).
* You can find other baselines in Github.ss
