'''
First training file using new format (check the prediction).
Can be trained using the *latest* deepjetcore (there was a minor change to allow for an arbitrary number of predictions for keras models).
A dataset can be found here: /eos/home-j/jkiesele/DeepNtuples/HGCal/Sept2020_19_production_1x1
'''
import tensorflow as tf
from argparse import ArgumentParser
# from K import Layer
import numpy as np
from tensorflow.keras.layers import BatchNormalization, Dropout, Add
from LayersRagged  import RaggedConstructTensor
from GravNetLayersRagged import ProcessFeatures,SoftPixelCNN, RaggedGravNet, DistanceWeightedMessagePassing
from tensorflow.keras.layers import Multiply, Dense, Concatenate, GaussianDropout
from DeepJetCore.modeltools import DJCKerasModel
from DeepJetCore.training.training_base import training_base
from tensorflow.keras import Model
from tensorboard_manager import TensorBoardManager
from running_plots import RunningEfficiencyFakeRateCallback

from DeepJetCore.modeltools import fixLayersContaining
# from tensorflow.keras.models import load_model
from DeepJetCore.training.training_base import custom_objects_list

# from tensorflow.keras.optimizer_v2 import Adam

from plotting_callbacks import plotEventDuringTraining
from ragged_callbacks import plotRunningPerformanceMetrics
from DeepJetCore.DJCLayers import StopGradient,ScalarMultiply, SelectFeatures, ReduceSumEntirely

from clr_callback import CyclicLR
from lossLayers import LLFullObjectCondensation, LLClusterCoordinates

from model_blocks import create_outputs

from Layers import LocalClusterReshapeFromNeighbours2,ManualCoordTransform,RaggedGlobalExchange,LocalDistanceScaling,CheckNaN,NeighbourApproxPCA,LocalClusterReshapeFromNeighbours,GraphClusterReshape, SortAndSelectNeighbours, LLLocalClusterCoordinates,DistanceWeightedMessagePassing,CollectNeighbourAverageAndMax,CreateGlobalIndices, LocalClustering, SelectFromIndices, MultiBackGather, KNN, MessagePassing, ExtendedMetricsModel, RobustModel
from datastructures import TrainData_OC 
td=TrainData_OC()
'''

'''

def gravnet_model(Inputs, 
                  beta_loss_scale,
                  q_min,
                  use_average_cc_pos,
                  kalpha_damping_strength,
                  batchnorm_momentum=0.9999 #that's actually the damping factor. High -> slow
                  ):
    
    feature_dropout=-1.
    addBackGatherInfo=True,
    
    feat,  t_idx, t_energy, t_pos, t_time, t_pid, row_splits = td.interpretAllModelInputs(Inputs)
    orig_t_idx, orig_t_energy, orig_t_pos, orig_t_time, orig_t_pid, orig_row_splits = t_idx, t_energy, t_pos, t_time, t_pid, row_splits
    gidx_orig = CreateGlobalIndices()(feat)
    
    _, row_splits = RaggedConstructTensor()([feat, row_splits])
    rs = row_splits
    
    feat_norm = ProcessFeatures()(feat)
    #feat_norm = BatchNormalization(momentum=batchnorm_momentum)(feat_norm)
    allfeat=[]
    x = feat_norm
    
    backgatheredids=[]
    gatherids=[]
    backgathered = []
    backgathered_coords = []
    energysums = []
    
    #really simple real coordinates
    energy = SelectFeatures(0,1)(feat)
    orig_coords = SelectFeatures(5,8)(feat)
    coords = ManualCoordTransform()(orig_coords)
    coords = Dense(3, use_bias=False, kernel_initializer=tf.keras.initializers.Identity()  )(coords)#just rotation and scaling
    
    
    #see whats there
    nidx, dist = KNN(K=64,radius=1.0)([coords,rs])
    
    x_c = Dense(32,activation='elu')(x)
    x_c = Dense(8)(x_c) #just a few features are enough here
    #this can be full blown because of the small number of input features
    x_c = NeighbourApproxPCA()([coords,dist,x_c,nidx])
    x_mp = DistanceWeightedMessagePassing([32,16,8])([x,nidx,dist])
    x = Concatenate()([x,x_c,x_mp])
    #this is going to be among the most expensive operations:
    x = Dense(64, activation='elu',name='pre_dense_a')(x)
    x = Dense(32, activation='selu',name='pre_dense_b')(x)
    x = BatchNormalization(momentum=batchnorm_momentum)(x)
    
    allfeat.append(x)
    backgathered_coords.append(coords)
    
    sel_gidx = gidx_orig
    
    cdist = dist
    ccoords = coords
    
    total_iterations=5
    
    collectedccoords=[]
    
    for i in range(total_iterations):
        
        cluster_neighbours = 5
        n_dimensions = 3 #make it plottable
        #derive new coordinates for clustering
        if i:
            ccoords = Add()([ccoords,ScalarMultiply(0.3)(Dense(n_dimensions,name='newcoords'+str(i),
                                         kernel_initializer='zeros'
                                         )(x))])
            nidx, cdist = KNN(K=6*cluster_neighbours,radius=-1.0)([ccoords,rs]) 
            #here we use more neighbours to improve learning of the cluster space
        
        
        #cluster first
        #hier = Dense(1,activation='sigmoid')(x)
        #distcorr = Dense(dist.shape[-1],activation='relu',kernel_initializer='zeros')(Concatenate()([x,dist]))
        #dist = Add()([distcorr,dist])
        cdist = LocalDistanceScaling(max_scale=5.)([cdist, Dense(1,kernel_initializer='zeros')(Concatenate()([x,cdist]))])
        #what if we let the individual distances scale here? so move hits in and out?
        
        x_cl, rs, bidxs, sel_gidx, energy, x, t_idx, coords, ccoords, cdist = LocalClusterReshapeFromNeighbours2(
                 K=cluster_neighbours, 
                 radius=0.1, 
                 print_reduction=False, 
                 loss_enabled=True, 
                 loss_scale = 1., 
                 loss_repulsion=0.4, #.5
                 hier_transforms=[64,32,32,32],
                 print_loss=False,
                 name='clustering_'+str(i)
                 )([x, cdist, nidx, rs, sel_gidx, energy, x, t_idx, coords, ccoords, cdist, t_idx])
        
        gatherids.append(bidxs)
        
        #explicit
        energy = ReduceSumEntirely()(energy)#sums up all contained energy per cluster
        #n_energy = BatchNormalization(momentum=batchnorm_momentum)(energy)
                 
        x = x_cl #Concatenate()([x_cl,n_energy])
        x = Dense(128, activation='elu',name='dense_clc_a'+str(i))(x)
        #x = BatchNormalization(momentum=batchnorm_momentum)(x)
        x = Dense(128, activation='elu',name='dense_clc_b'+str(i))(x)
        x = Dense(64, activation='selu')(x)
        x = BatchNormalization(momentum=batchnorm_momentum)(x)
        

        nneigh = 128
        nfilt = 64
        nprop = 64
        
        x = Concatenate()([coords,x])
        
        x_gn, coords, nidx, dist = RaggedGravNet(n_neighbours=nneigh,
                                              n_dimensions=n_dimensions,
                                              n_filters=nfilt,
                                              n_propagate=nprop)([x, rs])
                                              
                                              
        x_sp = Dense(16)(x)
        x_sp = NeighbourApproxPCA(hidden_nodes=[32,32,n_dimensions**2])([coords,
                                                                         dist,
                                                                         x_sp,nidx])
        x_sp = BatchNormalization(momentum=batchnorm_momentum)(x_sp)
        
        x_mp = DistanceWeightedMessagePassing([32,32,16,16,8,8])([x,nidx,dist])
        #x_mp = BatchNormalization(momentum=batchnorm_momentum)(x_mp)
        #x_sp=x_mp
        
        x = Concatenate()([x,x_mp,x_sp,x_gn])
        #check and compress it all                                      
        x = Dense(128, activation='elu',name='dense_a_'+str(i))(x)  
        #x = BatchNormalization(momentum=batchnorm_momentum)(x)    
        #x = Dense(128, activation='elu',name='dense_b_'+str(i))(x)
        x = Dense(64, activation='selu',name='dense_c_'+str(i))(x)
        x = Concatenate()([StopGradient()(ccoords),StopGradient()(cdist),x])
        x = BatchNormalization(momentum=batchnorm_momentum)(x)
        
         
        #record more and more the deeper we go
        x_r = x
        energysums.append( MultiBackGather()([energy, gatherids]) )#assign energy sum to all cluster components
        
        allfeat.append(MultiBackGather()([x_r, gatherids]))
        
        backgatheredids.append(MultiBackGather()([sel_gidx, gatherids]))
        backgathered_coords.append(MultiBackGather()([ccoords, gatherids]))  
          
        
        
    x = Concatenate(name='allconcat')(allfeat)
    x = Concatenate()([x]+energysums)
    x = Dense(128, activation='elu', name='alldense')(x)
    x = RaggedGlobalExchange()([x,row_splits])
    x = Dense(64, activation='selu')(x)
    x = BatchNormalization(momentum=batchnorm_momentum)(x)
    

    pred_beta, pred_ccoords, pred_energy, pred_pos, pred_time, pred_id = create_outputs(x,feat)
    
    #loss
    pred_beta = LLFullObjectCondensation(print_loss=True,
                                         energy_loss_weight=1e-1,
                                         position_loss_weight=1e-1,
                                         timing_loss_weight=1e-1,
                                         beta_loss_scale=beta_loss_scale,
                                         repulsion_scaling=1.,
                                         q_min=q_min,
                                         use_average_cc_pos=use_average_cc_pos,
                                         prob_repulsion=True,
                                         phase_transition=1,
                                         huber_energy_scale = 3,
                                         alt_potential_norm=True,
                                         payload_beta_gradient_damping_strength=0.,
                                         kalpha_damping_strength=kalpha_damping_strength,#1.,
                                         name="FullOCLoss"
                                         )([pred_beta, pred_ccoords, pred_energy, 
                                            pred_pos, pred_time, pred_id,
                                            orig_t_idx, orig_t_energy, orig_t_pos, orig_t_time, orig_t_pid,
                                            row_splits])

    return RobustModel(inputs=Inputs, outputs=[pred_beta,
                                         pred_ccoords,
                                         pred_energy, 
                                         pred_pos, 
                                         pred_time, 
                                         pred_id,
                                         rs]+backgatheredids+backgathered_coords)




parser = ArgumentParser('Run the training')
parser.add_argument("-b",  help="betascale", default=1., type=float)
parser.add_argument("-q",  help="qmin", default=1., type=float)
parser.add_argument("-a",  help="averaging strength", default=0.1, type=float)
parser.add_argument("-d",  help="kalpha damp", default=0., type=float)
        

train = training_base(parser=parser, testrun=False, resumeSilently=True, renewtokens=False)


if not train.modelSet():
    
    print('>>>>>>>>>>>>>\nsetting parameters to \nbeta_loss_scale',train.args.b)
    print('q_min',train.args.q)
    print('use_average_cc_pos',train.args.a)
    print('kalpha_damping_strength',train.args.d)
    print('<<<<<<<<<<<<<')

    train.setModel(gravnet_model,
                   beta_loss_scale = train.args.b,
                   q_min = train.args.q,
                   use_average_cc_pos = train.args.a,
                   kalpha_damping_strength = train.args.d
                   )
    train.setCustomOptimizer(tf.keras.optimizers.Nadam())

    train.compileModel(learningrate=1e-4,
                       loss=None)
    
    print(train.keras_model.summary())
    #exit()

verbosity = 2
import os

from plotting_callbacks import plotClusteringDuringTraining, plotGravNetCoordsDuringTraining

samplepath=train.val_data.getSamplePath(train.val_data.samples[0])
publishpath = 'jkiesele@lxplus.cern.ch:/eos/home-j/jkiesele/www/files/HGCalML_trainings/'+os.path.basename(os.path.normpath(train.outputDir))

plot_after_batches=2*250

cb = [plotClusteringDuringTraining(
           use_backgather_idx=7+i,
           outputfile=train.outputDir + "/plts/sn"+str(i)+'_',
           samplefile=  samplepath,
           after_n_batches=4*plot_after_batches,
           on_epoch_end=False,
           publish=publishpath+"_cl_"+str(i),
           use_event=0) 
    for i in [4]]

cb += [   
    plotEventDuringTraining(
            outputfile=train.outputDir + "/plts2/sn0",
            samplefile=samplepath,
            after_n_batches=plot_after_batches,
            batchsize=200000,
            on_epoch_end=False,
            publish = publishpath+"_event_"+ str(0),
            use_event=0)
    
    ]

cb += [   
    plotGravNetCoordsDuringTraining(
            outputfile=train.outputDir + "/coords_"+str(i)+"/coord_"+str(i),
            samplefile=samplepath,
            after_n_batches=4*plot_after_batches,
            batchsize=200000,  
            on_epoch_end=False,
            publish = publishpath+"_event_"+ str(0),
            use_event=0,
            use_prediction_idx=i,
            )
    for i in  range(12,18) #between 16 and 21
    ]

cb = []
os.system('mkdir -p %s' % (train.outputDir + "/summary/"))
tensorboard_manager = TensorBoardManager(train.outputDir + "/summary/")
cb += [RunningEfficiencyFakeRateCallback(td, tensorboard_manager, dist_threshold=0.5, beta_threshold=0.5)]

learningrate = 3e-3
nbatch = 100000 #quick first training with simple examples = low # hits

train.compileModel(learningrate=learningrate,
                          loss=None,
                          metrics=None,
                          clipnorm=0.001
                          )

model, history = train.trainModel(nepochs=1,
                                  run_eagerly=True,
                                  batchsize=nbatch,
                                  extend_truth_list_by = len(train.keras_model.outputs)-2, #just adapt truth list to avoid keras error (no effect on model)
                                  batchsize_use_sum_of_squares=False,
                                  checkperiod=1,  # saves a checkpoint model every N epochs
                                  verbose=verbosity,
                                  backup_after_batches=500,
                                  additional_callbacks=
                                  [CyclicLR (base_lr = learningrate/3.,
                                  max_lr = learningrate,
                                  step_size = 50)]+cb)

print("freeze BN")
for l in train.keras_model.layers:
    if 'atch_norm' in l.name:
        print('freezing', l.name)
        l.trainable=False #loss changes
        
#also stop GravNetLLLocalClusterLoss* from being evaluated
learningrate/=10.
nbatch = 110000

train.compileModel(learningrate=learningrate,
                          loss=None,
                          metrics=None)

model, history = train.trainModel(nepochs=121,
                                  run_eagerly=True,
                                  batchsize=nbatch,
                                  extend_truth_list_by = len(train.keras_model.outputs)-2, #just adapt truth list to avoid keras error (no effect on model)
                                  batchsize_use_sum_of_squares=False,
                                  checkperiod=1,  # saves a checkpoint model every N epochs
                                  verbose=verbosity,
                                  backup_after_batches=100,
                                  additional_callbacks=
                                  [CyclicLR (base_lr = learningrate/10.,
                                  max_lr = learningrate,
                                  step_size = 100)]+cb)

