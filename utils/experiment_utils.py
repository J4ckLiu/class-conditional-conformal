import glob # For getting file names
import numpy as np
import os
import pandas as pd
import pickle
import seaborn as sns

import pdb

from collections import Counter
from scipy import stats, cluster

from utils.clustering_utils import *
from utils.conformal_utils import *


# Used for processing iNaturalist dataset
def remove_rare_classes(softmax_scores, labels, thresh = 250):
    '''
    Filter out classes with fewer than thresh examples
    (removes full rows and softmax score entries corresponding to those classes)
    
    Note: Make sure to use raw softmax scores instead of 1-softmax in order for
    normalization to work correctly
    '''
    classes, cts = np.unique(labels, return_counts=True)
    non_rare_classes = classes[cts >= thresh]
    print(f'Data preprocessing: Keeping {len(non_rare_classes)} of {len(classes)} classes that have >= {thresh} examples')

    # Filter labels and re-index
    remaining_label_idx = np.isin(labels, non_rare_classes)
    labels = labels[remaining_label_idx]
    new_idx = 0
    mapping = {} # old to new
    for i, label in enumerate(labels):
        if label not in mapping:
            mapping[label] = new_idx
            new_idx += 1
        labels[i] = mapping[label]
    
    # Remove rows and columns corresponding to rare classes from scores matrix
    softmax_scores = softmax_scores[remaining_label_idx,:]
    new_softmax_scores = np.zeros((len(labels), len(non_rare_classes)))
    for k in non_rare_classes:
        new_softmax_scores[:, mapping[k]] = softmax_scores[:,k]
    
    # Renormalize each row to sum to 1 
    new_softmax_scores = new_softmax_scores / np.expand_dims(np.sum(new_softmax_scores, axis=1), axis=1)

    return new_softmax_scores, labels



def load_dataset(dataset, data_folder='data'):
    '''
    Load softmax scores and labels for a dataset
    
    Input:
        - dataset: string specifying dataset. Options are 'imagenet', 'cifar-100', 'places365', 'inaturalist'
        - data_folder: string specifying folder containing the <dataset name>.npz files

    Output: softmax_scores, labels
        
    '''
    assert dataset in ['imagenet', 'cifar-100', 'places365', 'inaturalist']
    
    
    data = np.load(f'{data_folder}/{dataset}.npz')
    softmax_scores = data['softmax']
    labels = data['labels']
    
    return softmax_scores, labels


def run_one_experiment(dataset, save_folder, alpha, n_totalcal, score_function_list, methods, seeds, 
                       cluster_args={'frac_clustering':'auto', 'num_clusters':'auto'},
                       save_preds=False, calibration_sampling='random', save_labels=False):
    '''
    Run experiment and save results
    
    Inputs:
        - dataset: string specifying dataset. Options are 'imagenet', 'cifar-100', 'places365', 'inaturalist'
        - n_totalcal: *average* number of examples per class. Calibration dataset is generated by sampling
          n_totalcal x num_classes examples uniformly at random
        - methods: List of conformal calibration methods. Options are 'standard', 'classwise', 
         'classwise_default_standard', 'cluster_proportional', 'cluster_doubledip','cluster_random'
         -cluster_args: Dict of arguments to be bassed into cluster_random
        - save_preds: if True, the val prediction sets are included in the saved outputs
        - calibration_sampling: Method for sampling calibration dataset. Options are 
        'random' or 'balanced'
        - save_labels: If True, save the labels for each random seed in {save_folder}seed={seed}_labels.npy
    '''
    np.random.seed(0)
    
    softmax_scores, labels = load_dataset(dataset)
    
    for score_function in score_function_list:
        curr_folder = os.path.join(save_folder, f'{dataset}/{calibration_sampling}_calset/n_totalcal={n_totalcal}/score={score_function}')
        os.makedirs(curr_folder, exist_ok=True)

        print(f'====== score_function={score_function} ======')

        print('Computing conformal score...')
        if score_function == 'softmax':
            scores_all = 1 - softmax_scores
        elif score_function == 'APS':
            scores_all = get_APS_scores_all(softmax_scores, randomize=True)
        elif score_function == 'RAPS': 
            # RAPS hyperparameters (currently using ImageNet defaults)
            lmbda = .01 
            kreg = 5

            scores_all = get_RAPS_scores_all(softmax_scores, lmbda, kreg, randomize=True)
        else:
            raise Exception('Undefined score function')

        for seed in seeds:
            print(f'\nseed={seed}')
            save_to = os.path.join(curr_folder, f'seed={seed}_allresults.pkl')
            if os.path.exists(save_to):
                with open(save_to,'rb') as f:
                    all_results = pickle.load(f)
                    print('Loaded existing results file containing results for', list(all_results.keys()))
            else:
                all_results = {} # Each value is (qhat(s), preds, coverage_metrics, set_size_metrics)

            # Split data
            if calibration_sampling == 'random':
                totalcal_scores_all, totalcal_labels, val_scores_all, val_labels = random_split(scores_all, 
                                                                                                labels, 
                                                                                                n_totalcal, 
                                                                                                seed=seed)
            elif calibration_sampling == 'balanced':
                num_classes = scores_all.shape[1]
                totalcal_scores_all, totalcal_labels, val_scores_all, val_labels = split_X_and_y(scores_all, 
                                                                                                labels, n_totalcal, num_classes, 
                                                                                                seed=seed, split='balanced')
            else:
                raise Exception('Invalid calibration_sampling option')
          
            # Inspect class imbalance of total calibration set
            cts = Counter(totalcal_labels).values()
            print(f'Class counts range from {min(cts)} to {max(cts)}')

            for method in methods:
                print(f'----- dataset={dataset}, n={n_totalcal},score_function={score_function}, seed={seed}, method={method} ----- ')

                if method == 'standard':
                    # Standard conformal
                    all_results[method] = standard_conformal(totalcal_scores_all, totalcal_labels, 
                                                             val_scores_all, val_labels, alpha)

                elif method == 'classwise':
                    # Classwise conformal  
                    all_results[method] = classwise_conformal(totalcal_scores_all, totalcal_labels, 
                                                               val_scores_all, val_labels, alpha, 
                                                               num_classes=totalcal_scores_all.shape[1],
                                                               default_qhat=np.inf, regularize=False)

                elif method == 'classwise_default_standard':
                    # Classwise conformal, but use standard qhat as default value instead of infinity 
                    all_results[method] = classwise_conformal(totalcal_scores_all, totalcal_labels, 
                                                               val_scores_all, val_labels, alpha, 
                                                               num_classes=totalcal_scores_all.shape[1],
                                                               default_qhat='standard', regularize=False)
                    
                elif method == 'cluster_proportional':
                    # Clustered conformal with proportionally sampled clustering set
                    all_results[method] = clustered_conformal(totalcal_scores_all, totalcal_labels,
                                                                alpha,
                                                                val_scores_all, val_labels, 
                                                                split='proportional')
                
                elif method == 'cluster_doubledip':
                    # Clustered conformal with double dipping for clustering and calibration
                    all_results[method] = clustered_conformal(totalcal_scores_all, totalcal_labels,
                                                               alpha,
                                                                val_scores_all, val_labels, 
                                                                split='doubledip')

                elif method == 'cluster_random':
                    # Clustered conformal with double dipping for clustering and calibration
                    all_results[method] = clustered_conformal(totalcal_scores_all, totalcal_labels,
                                                                alpha,
                                                                val_scores_all, val_labels, 
                                                                frac_clustering=cluster_args['frac_clustering'],
                                                                num_clusters=cluster_args['num_clusters'],
                                                                split='random')
                elif method == 'regularized_classwise':
                    # Empirical-Bayes-inspired regularized classwise conformal (shrink class qhats to standard)
                    all_results[method] = classwise_conformal(totalcal_scores_all, totalcal_labels, 
                                                               val_scores_all, val_labels, alpha, 
                                                               num_classes=totalcal_scores_all.shape[1],
                                                               default_qhat='standard', regularize=True)
                
                elif method == 'exact_coverage_standard':
                    # Apply randomization to qhat to achieve exact coverage
                    all_results[method] = standard_conformal(totalcal_scores_all, totalcal_labels,
                                                                            val_scores_all, val_labels, alpha,
                                                                            exact_coverage=True)
                    
                elif method == 'exact_coverage_classwise':
                    # Apply randomization to qhats to achieve exact coverage
                    all_results[method] = classwise_conformal(totalcal_scores_all, totalcal_labels, 
                                                               val_scores_all, val_labels, alpha, 
                                                               num_classes=totalcal_scores_all.shape[1],
                                                               default_qhat=np.inf, regularize=False,
                                                               exact_coverage=True)


                elif method == 'exact_coverage_cluster':
                    # Apply randomization to qhats to achieve exact coverage
                    all_results[method] = clustered_conformal(totalcal_scores_all, totalcal_labels,
                                                                alpha,
                                                                val_scores_all, val_labels, 
                                                                frac_clustering=cluster_args['frac_clustering'],
                                                                num_clusters=cluster_args['num_clusters'],
                                                                split='random',
                                                                exact_coverage=True)

                else: 
                    raise Exception('Invalid method selected')
            
            # Optionally remove predictions from saved output to reduce memory usage
            if not save_preds:
                for m in all_results.keys():
                    all_results[m] = (all_results[m][0], None, all_results[m][2], all_results[m][3])
                    
            # Optionally save val labels
            if save_labels:
                save_labels_to = os.path.join(curr_folder, f'seed={seed}_labels.npy')
                np.save(save_labels_to, val_labels)
                print(f'Saved labels to {save_labels_to}')
                
            # Save results 
            with open(save_to,'wb') as f:
                pickle.dump(all_results, f)
                print(f'Saved results to {save_to}')

# Helper function                
def initialize_metrics_dict(methods):
    
    metrics = {}
    for method in methods:
        metrics[method] = {'class_cov_gap': [],
                           'max_class_cov_gap': [],
                           'avg_set_size': [],
                           'marginal_cov': [],
                           'very_undercovered': []} # Could also retrieve other metrics
        
    return metrics


def average_results_across_seeds(folder, print_results=True, display_table=True, show_seed_ct=False, 
                                 methods=['standard', 'classwise', 'cluster_balanced'],
                                 max_seeds=np.inf):
    '''
    Input:
        - max_seeds: If we discover more than max_seeds random seeds, only use max_seeds of them
    '''

    
    file_names = sorted(glob.glob(os.path.join(folder, '*.pkl')))
    num_seeds = len(file_names)
    if show_seed_ct:
        print('Number of seeds found:', num_seeds)
    if max_seeds < np.inf and num_seeds > max_seeds:
        print(f'Only using {max_seeds} seeds')
        file_names = file_names[:max_seeds]
    
    metrics = initialize_metrics_dict(methods)
    
    for pth in file_names:
        with open(pth, 'rb') as f:
            results = pickle.load(f)
                        
        for method in methods:
            try:
                metrics[method]['class_cov_gap'].append(results[method][2]['mean_class_cov_gap'])
                metrics[method]['avg_set_size'].append(results[method][3]['mean'])
                metrics[method]['max_class_cov_gap'].append(results[method][2]['max_gap'])
                metrics[method]['marginal_cov'].append(results[method][2]['marginal_cov'])
                metrics[method]['very_undercovered'].append(results[method][2]['very_undercovered'])
            except:
                print(f'Missing {method} in {pth}')
            
#     print(folder)
#     for method in methods:
#         print(method, metrics[method]['class_cov_gap'])
            
    cov_means = []
    cov_ses = []
    set_size_means = []
    set_size_ses = []
    max_cov_gap_means = []
    max_cov_gap_ses = []
    marginal_cov_means = []
    marginal_cov_ses = []
    very_undercovered_means = []
    very_undercovered_ses = []
    
    if print_results:
        print('Avg class coverage gap for each random seed:')
    for method in methods:
        n = num_seeds
        if print_results:
            print(f'  {method}:', np.array(metrics[method]['class_cov_gap'])*100)
        cov_means.append(np.mean(metrics[method]['class_cov_gap']))
        cov_ses.append(np.std(metrics[method]['class_cov_gap'])/np.sqrt(n))
        
        set_size_means.append(np.mean(metrics[method]['avg_set_size']))
        set_size_ses.append(np.std(metrics[method]['avg_set_size'])/np.sqrt(n))
        
        max_cov_gap_means.append(np.mean(metrics[method]['max_class_cov_gap']))
        max_cov_gap_ses.append(np.std(metrics[method]['max_class_cov_gap'])/np.sqrt(n))
        
        marginal_cov_means.append(np.mean(metrics[method]['marginal_cov']))
        marginal_cov_ses.append(np.std(metrics[method]['marginal_cov'])/np.sqrt(n))
        
        very_undercovered_means.append(np.mean(metrics[method]['very_undercovered']))
        very_undercovered_ses.append(np.std(metrics[method]['very_undercovered'])/np.sqrt(n))
        
    df = pd.DataFrame({'method': methods,
                      'class_cov_gap_mean': np.array(cov_means)*100,
                      'class_cov_gap_se': np.array(cov_ses)*100,
                      'max_class_cov_gap_mean': np.array(max_cov_gap_means)*100,
                      'max_class_cov_gap_se': np.array(max_cov_gap_ses)*100,
                      'avg_set_size_mean': set_size_means,
                      'avg_set_size_se': set_size_ses,
                      'marginal_cov_mean': marginal_cov_means,
                      'marginal_cov_se': marginal_cov_ses,
                      'very_undercovered_mean': very_undercovered_means,
                      'very_undercovered_se': very_undercovered_ses})
    
    if display_table:
        display(df) # For Jupyter notebooks
        
    return df

# Helper function for get_metric_df
def initialize_dict(metrics, methods, suffixes=['mean', 'se']):
    d = {}
    for suffix in suffixes: 
        for metric in metrics:
            d[f'{metric}_{suffix}'] = {}

            for method in methods:

                d[f'{metric}_{suffix}'][method] = []
            
            
    return d

def get_metric_df(dataset, cal_sampling, metric, 
                  score_function,
                  method_list = ['standard', 'classwise', 'cluster_random'],
                  n_list = [10, 20, 30, 40, 50, 75, 100, 150],
                  show_seed_ct=False,
                  print_folder=True,
                  save_folder='../.cache/paper/varying_n'): # May have to update this path
    '''
    Similar to average_results_across_seeds
    '''
    
    aggregated_results = initialize_dict([metric], method_list)

    for n_totalcal in n_list:

        curr_folder = f'{save_folder}/{dataset}/{cal_sampling}_calset/n_totalcal={n_totalcal}/score={score_function}'
        if print_folder:
            print(curr_folder)

        df = average_results_across_seeds(curr_folder, print_results=False, 
                                          display_table=False, methods=method_list, max_seeds=10,
                                          show_seed_ct=show_seed_ct)

        for method in method_list:

            for suffix in ['mean', 'se']: # Extract mean and SE

                aggregated_results[f'{metric}_{suffix}'][method].append(df[f'{metric}_{suffix}'][df['method']==method].values[0])
  
    return aggregated_results

# Not used in paper
def plot_class_coverage_histogram(folder, desired_cov=None, vmin=.6, vmax=1, nbins=30, 
                                  title=None, methods=['standard', 'classwise', 'always_cluster']):
    '''
    For each method, aggregate class coverages across all random seeds and then 
    plot density/histogram. This is equivalent to estimating a density for each
    random seed individually then averaging. 
    
    Inputs:
    - folder: (str) containing path to folder of saved results
    - desired_cov: (float) Desired coverage level 
    - vmin, vmax: (floats) Specify bin edges 
    - 
    '''
    sns.set_style(style='white', rc={'axes.spines.right': False, 'axes.spines.top': False})
    sns.set_palette('pastel')
    sns.set_context('talk') # 'paper', 'talk', 'poster'
    
    # For plotting
    map_to_label = {'standard': 'Standard', 
                   'classwise': 'Classwise',
                   'cluster_random': 'Clustered',}
    map_to_color = {'standard': 'gray', 
                   'classwise': 'lightcoral',
                   'cluster_random': 'dodgerblue'}
    
    bin_edges = np.linspace(vmin,vmax,nbins+1)
    
    file_names = sorted(glob.glob(os.path.join(folder, '*.pkl')))
    num_seeds = len(file_names)
    print('Number of seeds found:', num_seeds)
    
    # OPTION 1: Plot average with 95% CIs
    cts_dict = {}
    for method in methods:
        cts_dict[method] = np.zeros((num_seeds, nbins))
        
    for i, pth in enumerate(file_names):
        with open(pth, 'rb') as f:
            results = pickle.load(f)
            
        for method in methods:
            
            cts, _ = np.histogram(results[method][2]['raw_class_coverages'], bins=bin_edges)
            cts_dict[method][i,:] = cts
    
    for method in methods:
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        graph = sns.lineplot(x=np.tile(bin_centers, num_seeds), y=np.ndarray.flatten(cts_dict[method]),
                     label=map_to_label[method], color=map_to_color[method])

    if desired_cov is not None:
        graph.axvline(desired_cov, color='black', linestyle='dashed', label='Desired coverage')
        
    plt.xlabel('Class-conditional coverage')
    plt.ylabel('Number of classes')
    plt.title(title)
    plt.ylim(bottom=0)
    plt.xlim(right=vmax)
    plt.legend()
    plt.show()
    
    # OPTION 2: Plot average, no CIs
#     class_coverages = {}
#     for method in methods:
#         class_coverages[method] = []
        
#     for pth in file_names:
#         with open(pth, 'rb') as f:
#             results = pickle.load(f)
            
#         for method in methods:
#             class_coverages[method].append(results[method][2]['raw_class_coverages'])
    
#     bin_edges = np.linspace(vmin,vmax,30) # Can adjust
    
#     for method in methods:
#         aggregated_scores = np.concatenate(class_coverages[method], axis=0)
#         cts, _ = np.histogram(aggregated_scores, bins=bin_edges, density=False)
#         cts = cts / num_seeds 
#         plt.plot((bin_edges[:-1] + bin_edges[1:]) / 2, cts, '-o', label=method, alpha=0.7)
        
#     plt.xlabel('Class-conditional coverage')
#     plt.ylabel('Number of classes')
#     plt.legend()

#     # OPTION 3: Plot separate lines
#     class_coverages = {}
#     for method in methods:
#         class_coverages[method] = []
        
#     for pth in file_names:
#         with open(pth, 'rb') as f:
#             results = pickle.load(f)
            
#         for method in methods:
#             class_coverages[method].append(results[method][2]['raw_class_coverages'])
    
#     bin_edges = np.linspace(vmin,vmax,30) # Can adjust
    
#     for method in methods:
#         for class_covs in class_coverages[method]:
#             cts, _ = np.histogram(class_covs, bins=bin_edges, density=False)
#             plt.plot((bin_edges[:-1] + bin_edges[1:]) / 2, cts, '-', alpha=0.3,
#                      label=map_to_label[method], color=map_to_color[method])
        
#     plt.xlabel('Class-conditional coverage')
#     plt.ylabel('Number of classes')
#     plt.show()
#     plt.legend()

# For square-root scaling in plots
import matplotlib.scale as mscale
import matplotlib.pyplot as plt
import matplotlib.transforms as mtransforms
import matplotlib.ticker as ticker
import numpy as np

class SquareRootScale(mscale.ScaleBase):
    """
    ScaleBase class for generating square root scale.
    """
 
    name = 'squareroot'
 
    def __init__(self, axis, **kwargs):
        # note in older versions of matplotlib (<3.1), this worked fine.
        # mscale.ScaleBase.__init__(self)

        # In newer versions (>=3.1), you also need to pass in `axis` as an arg
        mscale.ScaleBase.__init__(self, axis)
 
    def set_default_locators_and_formatters(self, axis):
        axis.set_major_locator(ticker.AutoLocator())
        axis.set_major_formatter(ticker.ScalarFormatter())
        axis.set_minor_locator(ticker.NullLocator())
        axis.set_minor_formatter(ticker.NullFormatter())
 
    def limit_range_for_scale(self, vmin, vmax, minpos):
        return  max(0., vmin), vmax
 
    class SquareRootTransform(mtransforms.Transform):
        input_dims = 1
        output_dims = 1
        is_separable = True
 
        def transform_non_affine(self, a): 
            return np.array(a)**0.5
 
        def inverted(self):
            return SquareRootScale.InvertedSquareRootTransform()
 
    class InvertedSquareRootTransform(mtransforms.Transform):
        input_dims = 1
        output_dims = 1
        is_separable = True
 
        def transform(self, a):
            return np.array(a)**2
 
        def inverted(self):
            return SquareRootScale.SquareRootTransform()
 
    def get_transform(self):
        return self.SquareRootTransform()
 
mscale.register_scale(SquareRootScale)
    